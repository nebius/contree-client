"""Build the language-neutral intermediate representation from OpenAPI."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any

from .loader import Spec, ref_name
from .naming import pascal_case, snake_case

# Schemas that never become model definitions.
SKIP_SCHEMAS = {
    "UUIDSchema",
    "ImageSourceTag",
    "ImageSource",
    "OperationEventStream",
    "OperationEventSSEFrame",
    "OperationMetadata",
    "OperationEventType",
}
STRING_ALIAS_SCHEMAS = {"UUIDSchema", "ImageSourceTag", "ImageSource"}

NESTED_NAME_OVERRIDES = {
    ("InstanceSpawnRequest", "files"): "FileSpec",
    ("InstanceSpawnResponse", "files"): "FileSpec",
    ("OperationInstanceMetadata", "files"): "FileSpec",
    ("OperationResponse", "result"): "OperationResult",
}

SKIP_OPERATIONS = {"inspectRedirect", "inspectImageRedirect"}
# archives can be arbitrarily large: stream-only, never buffered
STREAM_ONLY_OPERATIONS = {"inspectImageArchive"}
# The server accepts duplicate keys for these parameters, but the current
# OpenAPI document still declares each item as a scalar string.
REPEATABLE_QUERY_PARAMS = {
    ("inspect_image_grep", "pattern"),
    ("inspect_image_grep", "path"),
    ("inspect_image_grep", "glob"),
}
OPERATION_NAME_OVERRIDES = {
    "whoAmI": "whoami",
    "getOperationEvents": "iter_operation_events",
    "inspectImageFile": "inspect_image_list",
}
SYNTHETIC_OPERATION_IDS = {
    ("get", "/files"): "listFiles",
    ("get", "/files/{sha256}"): "getFile",
}
SKIP_PARAMS = {"text"}
HTTP_METHODS = ("get", "put", "post", "delete", "patch", "head", "options")


# ---------------------------------------------------------------------------
# Type references
# ---------------------------------------------------------------------------


class TypeKind(Enum):
    """Language-neutral value categories understood by every emitter."""

    ANY = auto()
    STRING = auto()
    INTEGER = auto()
    NUMBER = auto()
    BOOLEAN = auto()
    DATETIME = auto()
    BYTES = auto()
    BINARY_STREAM = auto()
    MODEL = auto()
    ENUM = auto()
    ALIAS = auto()
    LITERAL = auto()
    LIST = auto()
    SEQUENCE = auto()
    MAP = auto()
    UNION = auto()


@dataclass(frozen=True)
class TypeRef:
    """A semantic type expression with no target-language syntax."""

    kind: TypeKind
    name: str | None = None
    arguments: tuple[TypeRef, ...] = ()
    values: tuple[str, ...] = ()


STR = TypeRef(TypeKind.STRING)
INT = TypeRef(TypeKind.INTEGER)
FLOAT = TypeRef(TypeKind.NUMBER)
BOOL = TypeRef(TypeKind.BOOLEAN)
ANY = TypeRef(TypeKind.ANY)
DATETIME = TypeRef(TypeKind.DATETIME)
BYTES = TypeRef(TypeKind.BYTES)
BINARY_STREAM = TypeRef(TypeKind.BINARY_STREAM)


def model_ref(name: str) -> TypeRef:
    return TypeRef(TypeKind.MODEL, name=name)


def enum_ref(name: str) -> TypeRef:
    return TypeRef(TypeKind.ENUM, name=name)


def alias_ref(name: str) -> TypeRef:
    return TypeRef(TypeKind.ALIAS, name=name)


def list_of(item: TypeRef) -> TypeRef:
    return TypeRef(TypeKind.LIST, arguments=(item,))


def sequence_of(item: TypeRef) -> TypeRef:
    return TypeRef(TypeKind.SEQUENCE, arguments=(item,))


def dict_of(value: TypeRef) -> TypeRef:
    return TypeRef(TypeKind.MAP, arguments=(value,))


def literal_of(values: list[str]) -> TypeRef:
    return TypeRef(TypeKind.LITERAL, values=tuple(values))


def union_of(*variants: TypeRef) -> TypeRef:
    unique: list[TypeRef] = []
    for variant in variants:
        if variant not in unique:
            unique.append(variant)
    if len(unique) == 1:
        return unique[0]
    return TypeRef(TypeKind.UNION, arguments=tuple(unique))


OPERATION_STATUS = enum_ref("OperationStatus")
OPERATION_EVENT_TYPE = alias_ref("OperationEventType")


EVENT_DATA_VARIANTS = (
    ("init", model_ref("EventDataInit")),
    ("spawn", model_ref("EventDataSpawn")),
    ("stdin", model_ref("EventDataStream")),
    ("stdout", model_ref("EventDataStream")),
    ("stderr", model_ref("EventDataStream")),
    ("exit", model_ref("EventDataExit")),
    ("truncated", model_ref("EventDataTruncated")),
    ("size_cap", model_ref("EventDataSizeCap")),
    ("network", model_ref("EventDataNetwork")),
    ("shutdown", model_ref("EventDataShutdown")),
    ("completion", model_ref("EventDataCompletion")),
)


@dataclass(frozen=True)
class DiscriminatorDef:
    """Select a field type from another wire field in the same model."""

    parent_field: str
    cases: tuple[tuple[str, TypeRef], ...]
    fallback: TypeRef
    name: str | None = None


@dataclass(frozen=True)
class FieldTypeOverride:
    type: TypeRef
    discriminator: DiscriminatorDef | None = None


@dataclass(frozen=True)
class Documentation:
    """Raw documentation data that emitters format for their target."""

    description: str = ""
    example: Any = None
    has_example: bool = False


# Explicit strategies for unions the generic rules cannot express:
# discriminated unions (no machine-readable discriminator in the spec)
# and deliberately untyped payloads.
FIELD_TYPE_OVERRIDES: dict[tuple[str, str], FieldTypeOverride] = {
    ("Error", "error"): FieldTypeOverride(ANY),
    # the wire format is an octal string; the constructor also accepts
    # a plain int (0o644) which __post_init__ normalizes
    ("FileSpec", "mode"): FieldTypeOverride(union_of(STR, INT)),
    ("OperationResponse", "metadata"): FieldTypeOverride(
        union_of(
            model_ref("OperationInstanceMetadata"),
            model_ref("ImageImportMetadata"),
        ),
        DiscriminatorDef(
            parent_field="kind",
            cases=(("instance", model_ref("OperationInstanceMetadata")),),
            fallback=model_ref("ImageImportMetadata"),
        ),
    ),
    ("OperationEvent", "data"): FieldTypeOverride(
        union_of(alias_ref("EventData"), dict_of(ANY)),
        DiscriminatorDef(
            parent_field="type",
            cases=EVENT_DATA_VARIANTS,
            fallback=dict_of(ANY),
            name="EventData",
        ),
    ),
}


class ModelTrait(Enum):
    """Shared model behavior rendered idiomatically by each language."""

    STREAM_VALUE = auto()
    FILE_MODE = auto()


MODEL_TRAITS: dict[str, frozenset[ModelTrait]] = {
    "StreamRepr": frozenset({ModelTrait.STREAM_VALUE}),
    "EventDataStream": frozenset({ModelTrait.STREAM_VALUE}),
    "FileSpec": frozenset({ModelTrait.FILE_MODE}),
}


# ---------------------------------------------------------------------------
# Dataclass model definitions
# ---------------------------------------------------------------------------


@dataclass
class FieldDef:
    """One model field.

    Optional fields are tri-state: absent, explicit null, or a value.
    Required nullable fields accept either null or a value.
    """

    name: str
    wire_name: str
    type: TypeRef
    required: bool
    nullable: bool
    discriminator: DiscriminatorDef | None = None
    documentation: Documentation = Documentation()
    default_value: Any = None
    has_default: bool = False


@dataclass
class ModelDef:
    name: str
    description: str
    fields: list[FieldDef]
    traits: frozenset[ModelTrait] = frozenset()


# ---------------------------------------------------------------------------
# Schema conversion
# ---------------------------------------------------------------------------


class SchemaConverter:
    def __init__(self, spec: Spec) -> None:
        self.spec = spec
        self.models: list[ModelDef] = []
        self.known: set[str] = set()
        self.status_values = operation_status_values(spec)

    def convert_all(self) -> None:
        for name, schema in self.spec.schemas.items():
            if name in SKIP_SCHEMAS:
                continue
            self.convert_object(name, schema)

    def model_by_name(self, name: str) -> ModelDef:
        for model in self.models:
            if model.name == name:
                return model
        raise KeyError(name)

    def convert_object(self, name: str, schema: dict[str, Any]) -> str:
        if name in self.known:
            return name
        self.known.add(name)
        merged = self.merge_allof(schema)
        required = set(merged.get("required", []))
        fields: list[FieldDef] = []
        for json_name, prop in merged.get("properties", {}).items():
            type_ref, nullable = self.field_type(name, json_name, prop)
            override = FIELD_TYPE_OVERRIDES.get((name, json_name))
            field_name = snake_case(json_name)
            fields.append(
                FieldDef(
                    name=field_name,
                    wire_name=json_name,
                    type=type_ref,
                    required=json_name in required,
                    nullable=nullable,
                    discriminator=(
                        override.discriminator if override is not None else None
                    ),
                    documentation=Documentation(
                        description=str(prop.get("description", "")).strip(),
                        example=prop.get("example"),
                        has_example="example" in prop,
                    ),
                    default_value=prop.get("default"),
                    has_default="default" in prop,
                )
            )
        self.models.append(
            ModelDef(
                name=name,
                description=schema.get("description", ""),
                fields=fields,
                traits=MODEL_TRAITS.get(name, frozenset()),
            )
        )
        return name

    def merge_allof(self, schema: dict[str, Any]) -> dict[str, Any]:
        if "allOf" not in schema:
            return self.spec.deref(schema)
        properties: dict[str, Any] = {}
        required: list[str] = []
        for part in schema["allOf"]:
            merged = self.merge_allof(self.spec.deref(part))
            properties.update(merged.get("properties", {}))
            for item in merged.get("required", []):
                if item not in required:
                    required.append(item)
        return {"type": "object", "properties": properties, "required": required}

    def field_type(
        self,
        parent: str,
        json_name: str,
        schema: dict[str, Any],
    ) -> tuple[TypeRef, bool]:
        nullable = bool(schema.get("nullable"))
        override = FIELD_TYPE_OVERRIDES.get((parent, json_name))
        if override is not None:
            return override.type, nullable

        rname = ref_name(schema)
        if rname is not None:
            return self.type_for_named(rname), nullable

        if "allOf" in schema:
            parts = schema["allOf"]
            if len(parts) == 1:
                inner, inner_nullable = self.field_type(parent, json_name, parts[0])
                return inner, nullable or inner_nullable
            merged = self.merge_allof(schema)
            nested = self.nested_class(parent, json_name, merged)
            return model_ref(nested), nullable

        for union_keyword in ("oneOf", "anyOf"):
            if union_keyword not in schema:
                continue
            scalars = {
                "string": STR,
                "integer": INT,
                "number": FLOAT,
                "boolean": BOOL,
            }
            variants: list[TypeRef] = []
            for variant in schema[union_keyword]:
                resolved = self.spec.deref(variant)
                mapped = scalars.get(str(resolved.get("type")))
                if mapped is None:
                    # discriminated/object unions cannot be inferred:
                    # they need an explicit parse strategy
                    raise ValueError(
                        f"{parent}.{json_name}: unsupported {union_keyword}"
                        f" variant of type {resolved.get('type')!r};"
                        " add a FIELD_TYPE_OVERRIDES entry"
                    )
                if mapped not in variants:
                    variants.append(mapped)
            return union_of(*variants), nullable

        schema_type = schema.get("type")
        if schema_type == "string":
            if schema.get("enum"):
                values = [v for v in schema["enum"] if isinstance(v, str)]
                if json_name == "status" and set(values) <= set(self.status_values):
                    return OPERATION_STATUS, nullable
                return literal_of(values), nullable
            if schema.get("format") == "date-time":
                return DATETIME, nullable
            return STR, nullable
        if schema_type == "integer":
            return INT, nullable
        if schema_type == "number":
            return FLOAT, nullable
        if schema_type == "boolean":
            return BOOL, nullable
        if schema_type == "array":
            item, _ = self.field_type(parent, json_name, schema["items"])
            return list_of(item), nullable
        if schema_type == "object":
            if schema.get("properties"):
                nested = self.nested_class(parent, json_name, schema)
                return model_ref(nested), nullable
            additional = schema.get("additionalProperties")
            if isinstance(additional, dict):
                value, _ = self.field_type(parent, json_name, additional)
                return dict_of(value), nullable
            return dict_of(ANY), nullable
        return ANY, nullable

    def type_for_named(self, rname: str) -> TypeRef:
        if rname in STRING_ALIAS_SCHEMAS:
            return STR
        if rname == "OperationEventType":
            return OPERATION_EVENT_TYPE
        schema = self.spec.schemas[rname]
        if schema.get("type") == "string":
            return STR
        self.convert_object(rname, schema)
        return model_ref(rname)

    def nested_class(
        self,
        parent: str,
        json_name: str,
        schema: dict[str, Any],
    ) -> str:
        name = NESTED_NAME_OVERRIDES.get((parent, json_name))
        if name is None:
            base = parent.removesuffix("Request")
            name = base + pascal_case(json_name)
        return self.convert_object(name, schema)


# ---------------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------------


class ArgumentPresence(Enum):
    """How an API argument is omitted when callers do not supply it."""

    REQUIRED = auto()
    OMIT_IF_NULL = auto()
    OMIT_IF_FALSE = auto()
    OMIT_IF_UNSET = auto()


@dataclass
class ArgumentDef:
    name: str
    type: TypeRef
    presence: ArgumentPresence
    nullable: bool = False
    documentation: Documentation = Documentation()

    @property
    def required(self) -> bool:
        return self.presence is ArgumentPresence.REQUIRED


class ParameterLocation(Enum):
    PATH = auto()
    QUERY = auto()
    HEADER = auto()


class ParameterEncoding(Enum):
    IDENTITY = auto()
    STRING = auto()
    ONE_IF_TRUE = auto()
    TIME = auto()


@dataclass(frozen=True)
class ParameterDef:
    wire_name: str
    argument: str
    location: ParameterLocation
    encoding: ParameterEncoding = ParameterEncoding.IDENTITY
    repeatable: bool = False


class BodyKind(Enum):
    JSON_MODEL = auto()
    JSON_INLINE = auto()
    BINARY = auto()


@dataclass(frozen=True)
class BodyBinding:
    wire_name: str
    argument: str


@dataclass
class BodyDef:
    kind: BodyKind
    bindings: list[BodyBinding] = field(default_factory=list)
    model: TypeRef | None = None


@dataclass
class RequestDef:
    parameters: list[ParameterDef] = field(default_factory=list)
    body: BodyDef | None = None
    idempotent: bool = False
    accept: str | None = None


class ResponseMode(Enum):
    JSON = auto()
    BYTES = auto()
    EMPTY = auto()
    STATUS_BOOL = auto()
    LOCATION = auto()
    SSE = auto()
    BYTE_STREAM = auto()


class SuccessPolicy(Enum):
    ANY_2XX = auto()
    EXACT = auto()


@dataclass(frozen=True)
class ResponseDef:
    mode: ResponseMode
    type: TypeRef | None = None
    success: SuccessPolicy = SuccessPolicy.ANY_2XX
    success_statuses: tuple[int, ...] = ()
    false_statuses: tuple[int, ...] = ()
    json_path: tuple[str, ...] = ()
    header_name: str | None = None
    resume_argument: str | None = None


@dataclass(frozen=True)
class PaginationDef:
    iterator_name: str
    item_type: TypeRef
    items_path: tuple[str, ...]
    limit_argument: str
    offset_argument: str
    max_page_size: int


@dataclass
class OperationDef:
    name: str
    http_method: str
    path: str
    summary: str
    description: str = ""
    arguments: list[ArgumentDef] = field(default_factory=list)
    request: RequestDef = field(default_factory=RequestDef)
    response: ResponseDef = field(
        default_factory=lambda: ResponseDef(ResponseMode.EMPTY)
    )
    pagination: PaginationDef | None = None


PAGINATED_OPERATIONS: dict[str, tuple[str, TypeRef, tuple[str, ...]]] = {
    "list_images": ("iter_images", model_ref("Image"), ("images",)),
    "list_operations": (
        "iter_operations",
        model_ref("OperationSummary"),
        (),
    ),
    "list_files": ("iter_files", model_ref("File"), ("files",)),
}


class OperationBuilder:
    def __init__(self, spec: Spec, converter: SchemaConverter) -> None:
        self.spec = spec
        self.converter = converter

    def build_all(self) -> list[OperationDef]:
        operations: list[OperationDef] = []
        for path, item in self.spec.paths.items():
            shared_params = item.get("parameters", [])
            for method in HTTP_METHODS:
                raw = item.get(method)
                if raw is None:
                    continue
                op_id = (
                    raw.get("operationId") or SYNTHETIC_OPERATION_IDS[(method, path)]
                )
                if op_id in SKIP_OPERATIONS:
                    continue
                operations.append(
                    self.build_operation(op_id, method, path, raw, shared_params)
                )
        return operations

    def build_operation(
        self,
        op_id: str,
        method: str,
        path: str,
        raw: dict[str, Any],
        shared_params: list[dict[str, Any]],
    ) -> OperationDef:
        name = OPERATION_NAME_OVERRIDES.get(op_id) or snake_case(op_id)
        operation = OperationDef(
            name=name,
            http_method=method.upper(),
            path=path,
            summary=raw.get("summary", ""),
            description=str(raw.get("description", "")).strip(),
            request=RequestDef(
                idempotent=method.upper() in ("GET", "HEAD", "PUT", "DELETE")
            ),
        )

        path_arguments: list[ArgumentDef] = []
        page_size_max: int | None = None

        params = [
            self.spec.deref(p) for p in list(shared_params) + raw.get("parameters", [])
        ]
        for param in params:
            if param["name"] in SKIP_PARAMS:
                continue
            if param["name"] == "limit":
                maximum = (param.get("schema") or {}).get("maximum")
                if isinstance(maximum, int):
                    page_size_max = maximum
            self.add_parameter(operation, param, path_arguments)

        self.add_body(operation, raw)

        response = self.build_response(op_id, method, raw["responses"])
        if op_id in STREAM_ONLY_OPERATIONS:
            response = ResponseDef(
                mode=ResponseMode.BYTE_STREAM,
                type=BYTES,
                success=response.success,
                success_statuses=response.success_statuses,
            )
        operation.response = response
        if response.mode is ResponseMode.SSE:
            operation.request.accept = "text/event-stream"

        operation.arguments = path_arguments + operation.arguments

        pagination = PAGINATED_OPERATIONS.get(name)
        if pagination is not None:
            iterator_name, item_type, items_path = pagination
            operation.pagination = PaginationDef(
                iterator_name=iterator_name,
                item_type=item_type,
                items_path=items_path,
                limit_argument="limit",
                offset_argument="offset",
                max_page_size=page_size_max or 1000,
            )
        return operation

    def add_parameter(
        self,
        operation: OperationDef,
        param: dict[str, Any],
        path_arguments: list[ArgumentDef],
    ) -> None:
        wire_name = param["name"]
        location = param["in"]
        schema = self.spec.deref(param.get("schema", {}))
        name = snake_case(wire_name)
        example_source = param if "example" in param else schema
        documentation = Documentation(
            description=str(param.get("description", "")).strip(),
            example=example_source.get("example"),
            has_example="example" in example_source,
        )

        if location == "path":
            type_ref = INT if schema.get("type") == "integer" else STR
            path_arguments.append(
                ArgumentDef(
                    name,
                    type_ref,
                    ArgumentPresence.REQUIRED,
                    documentation=documentation,
                )
            )
            operation.request.parameters.append(
                ParameterDef(wire_name, name, ParameterLocation.PATH)
            )
            return

        if location == "header":
            operation.arguments.append(
                ArgumentDef(
                    name,
                    INT,
                    ArgumentPresence.OMIT_IF_NULL,
                    nullable=True,
                    documentation=documentation,
                )
            )
            operation.request.parameters.append(
                ParameterDef(
                    wire_name,
                    name,
                    ParameterLocation.HEADER,
                    ParameterEncoding.STRING,
                )
            )
            return

        is_flag = schema.get("enum") == ["", "0", "1"] or wire_name == "tagged"
        if is_flag:
            operation.arguments.append(
                ArgumentDef(
                    name,
                    BOOL,
                    ArgumentPresence.OMIT_IF_FALSE,
                    documentation=documentation,
                )
            )
            operation.request.parameters.append(
                ParameterDef(
                    wire_name,
                    name,
                    ParameterLocation.QUERY,
                    ParameterEncoding.ONE_IF_TRUE,
                )
            )
            return

        if wire_name in ("since", "until") and schema.get("type") == "string":
            operation.arguments.append(
                ArgumentDef(
                    name,
                    union_of(STR, INT, FLOAT, DATETIME),
                    ArgumentPresence.OMIT_IF_NULL,
                    nullable=True,
                    documentation=documentation,
                )
            )
            operation.request.parameters.append(
                ParameterDef(
                    wire_name,
                    name,
                    ParameterLocation.QUERY,
                    ParameterEncoding.TIME,
                )
            )
            return

        required = bool(param.get("required"))
        repeatable = (operation.name, wire_name) in REPEATABLE_QUERY_PARAMS
        if repeatable:
            type_ref = union_of(STR, sequence_of(STR))
            encoding = ParameterEncoding.IDENTITY
        elif schema.get("type") == "integer":
            type_ref = INT
            encoding = ParameterEncoding.STRING
        elif schema.get("enum"):
            values = schema["enum"]
            if wire_name == "status" and set(values) <= set(
                self.converter.status_values
            ):
                type_ref = union_of(OPERATION_STATUS, STR)
                encoding = ParameterEncoding.STRING
            else:
                type_ref = literal_of(values)
                encoding = ParameterEncoding.IDENTITY
        else:
            type_ref = STR
            encoding = ParameterEncoding.IDENTITY
        operation.arguments.append(
            ArgumentDef(
                name,
                type_ref,
                (
                    ArgumentPresence.REQUIRED
                    if required
                    else ArgumentPresence.OMIT_IF_NULL
                ),
                nullable=not required,
                documentation=documentation,
            )
        )
        operation.request.parameters.append(
            ParameterDef(
                wire_name,
                name,
                ParameterLocation.QUERY,
                encoding,
                repeatable=repeatable,
            )
        )

    def add_body(
        self,
        operation: OperationDef,
        raw: dict[str, Any],
    ) -> None:
        request_body = raw.get("requestBody")
        if not request_body:
            return
        content = request_body["content"]
        if "application/octet-stream" in content:
            operation.arguments.append(
                ArgumentDef(
                    "content",
                    union_of(BYTES, BINARY_STREAM),
                    ArgumentPresence.REQUIRED,
                    documentation=Documentation(
                        description="Raw file content: bytes or a binary stream."
                    ),
                )
            )
            operation.request.body = BodyDef(
                BodyKind.BINARY,
                bindings=[BodyBinding("content", "content")],
            )
            return
        schema = content["application/json"]["schema"]
        rname = ref_name(schema)
        if rname is not None:
            model = self.converter.model_by_name(rname)
            ordered_fields = [field for field in model.fields if field.required]
            ordered_fields.extend(field for field in model.fields if not field.required)
            bindings: list[BodyBinding] = []
            for field_def in ordered_fields:
                operation.arguments.append(
                    ArgumentDef(
                        field_def.name,
                        field_def.type,
                        (
                            ArgumentPresence.REQUIRED
                            if field_def.required
                            else ArgumentPresence.OMIT_IF_UNSET
                        ),
                        nullable=field_def.nullable,
                        documentation=field_def.documentation,
                    )
                )
                bindings.append(BodyBinding(field_def.wire_name, field_def.name))
            operation.request.body = BodyDef(
                BodyKind.JSON_MODEL,
                bindings=bindings,
                model=model_ref(rname),
            )
            return

        required = set(schema.get("required", []))
        bindings: list[BodyBinding] = []
        for wire_name, prop in schema.get("properties", {}).items():
            type_ref, nullable = self.converter.field_type("", wire_name, prop)
            name = snake_case(wire_name)
            operation.arguments.append(
                ArgumentDef(
                    name,
                    type_ref,
                    (
                        ArgumentPresence.REQUIRED
                        if wire_name in required
                        else ArgumentPresence.OMIT_IF_UNSET
                    ),
                    nullable=nullable,
                    documentation=Documentation(
                        description=str(prop.get("description", "")).strip(),
                        example=prop.get("example"),
                        has_example="example" in prop,
                    ),
                )
            )
            bindings.append(BodyBinding(wire_name, name))
        operation.request.body = BodyDef(BodyKind.JSON_INLINE, bindings=bindings)

    def build_response(
        self,
        op_id: str,
        method: str,
        responses: dict[str, Any],
    ) -> ResponseDef:
        if method == "head":
            return ResponseDef(
                ResponseMode.STATUS_BOOL,
                type=BOOL,
                success_statuses=(200,),
                false_statuses=(404,),
            )
        if op_id == "inspectFindImageByTag":
            return ResponseDef(
                ResponseMode.LOCATION,
                type=STR,
                success=SuccessPolicy.EXACT,
                success_statuses=(302,),
                header_name="location",
            )
        for code in ("200", "201"):
            response = responses.get(code)
            if response is None:
                continue
            content = response.get("content") or {}
            if "text/event-stream" in content:
                return ResponseDef(
                    ResponseMode.SSE,
                    type=model_ref("OperationEvent"),
                    success_statuses=(int(code),),
                    resume_argument="last_event_id",
                )
            if "application/octet-stream" in content or "application/x-tar" in content:
                return ResponseDef(
                    ResponseMode.BYTES,
                    type=BYTES,
                    success_statuses=(int(code),),
                )
            if "application/json" in content:
                schema = content["application/json"]["schema"]
                if schema.get("type") == "array":
                    item = ref_name(schema["items"])
                    if item is None:
                        raise ValueError(f"unsupported array response in {op_id}")
                    return ResponseDef(
                        ResponseMode.JSON,
                        type=list_of(model_ref(item)),
                        success_statuses=(int(code),),
                    )
                rname = ref_name(schema)
                if rname is not None:
                    return ResponseDef(
                        ResponseMode.JSON,
                        type=model_ref(rname),
                        success_statuses=(int(code),),
                    )
                scalar = self.single_scalar_field(schema)
                if scalar is None:
                    raise ValueError(f"unsupported inline response in {op_id}")
                field_name, type_ref = scalar
                return ResponseDef(
                    ResponseMode.JSON,
                    type=type_ref,
                    success_statuses=(int(code),),
                    json_path=(field_name,),
                )
            return ResponseDef(
                ResponseMode.EMPTY,
                success_statuses=(int(code),),
            )
        for code in ("204", "202"):
            if code in responses:
                return ResponseDef(
                    ResponseMode.EMPTY,
                    success_statuses=(int(code),),
                )
        raise ValueError(f"cannot infer response kind for {op_id}")

    def single_scalar_field(self, schema: dict[str, Any]) -> tuple[str, TypeRef] | None:
        """Return the name and type of a lone required scalar property.

        Some operations answer with a one-field envelope instead of a
        named model — ``importImage`` (``{"uuid": ...}``) and
        ``operationSubprocessCreate`` (``{"spid": ...}``); the wrapped
        scalar is the value callers actually want, so the client
        unwraps it.  Returns None for any other inline shape.
        """
        if schema.get("type") != "object":
            return None
        required = schema.get("required") or []
        properties = schema.get("properties") or {}
        if len(required) != 1 or set(properties) != set(required):
            return None
        (name,) = required
        prop = self.spec.deref(properties[name])
        type_ref = {"string": STR, "integer": INT}.get(prop.get("type", ""))
        if type_ref is None:
            return None
        return name, type_ref


# ---------------------------------------------------------------------------
# Whole-spec IR
# ---------------------------------------------------------------------------


@dataclass
class SpecIR:
    default_base_url: str
    spec_text: str
    spec_sha256: str
    models: list[ModelDef]
    operations: list[OperationDef]
    event_type_values: list[str]
    status_values: list[str]
    terminal_status_values: list[str]
    event_data_variants: tuple[tuple[str, TypeRef], ...] = EVENT_DATA_VARIANTS

    @property
    def model_names(self) -> list[str]:
        return [model.name for model in self.models]


def operation_status_values(spec: Spec) -> list[str]:
    summary = spec.schemas["OperationSummary"]
    values: list[str] = list(summary["properties"]["status"]["enum"])
    return values


def terminal_status_values(spec: Spec) -> list[str]:
    completion = spec.schemas["EventDataCompletion"]
    values: list[str] = list(completion["properties"]["status"]["enum"])
    return values


def build_ir(spec: Spec) -> SpecIR:
    converter = SchemaConverter(spec)
    converter.convert_all()
    operations = OperationBuilder(spec, converter).build_all()
    event_type_values = list(spec.schemas["OperationEventType"]["enum"])
    return SpecIR(
        default_base_url=spec.default_base_url,
        spec_text=spec.text,
        spec_sha256=spec.sha256,
        models=converter.models,
        operations=operations,
        event_type_values=event_type_values,
        status_values=converter.status_values,
        terminal_status_values=terminal_status_values(spec),
    )
