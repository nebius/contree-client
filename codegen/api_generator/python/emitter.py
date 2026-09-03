"""Rendering of the generated Python contree_client package."""

from __future__ import annotations

import importlib.util
import py_compile
import re
import subprocess
import sys
from collections.abc import Iterable
from pathlib import Path

from api_generator.documentation import (
    build_docstring,
    description_body,
    documentation_text,
    render_docstring,
)
from api_generator.emitter import GENERATED_NOTE, Emitter
from api_generator.ir import (
    ArgumentDef,
    ArgumentPresence,
    BodyKind,
    Documentation,
    FieldDef,
    ModelDef,
    ModelTrait,
    OperationDef,
    ParameterDef,
    ParameterEncoding,
    ParameterLocation,
    ResponseMode,
    SpecIR,
    SuccessPolicy,
    TypeKind,
    TypeRef,
)

INDENT = "    "

# Files the generator owns inside the otherwise static package.
GENERATED_FILES = (
    "__init__.py",
    "base.py",
    "models.py",
    "operations.py",
    "spec_info.py",
)

EVENT_DATA_HELPERS = '''
def parse_event_data(
    event_type: str,
    data: Any,
) -> EventData | Any:
    """Decode a per-type event payload.

    Unknown event types and payloads that do not match the documented
    schema - a non-mapping body included - are returned as-is instead
    of failing the stream.
    """
    if not isinstance(data, dict):
        return data
    parser = EVENT_DATA_PARSERS.get(event_type)
    if parser is None:
        return data
    try:
        return parser(data)
    except (KeyError, TypeError, ValueError):
        return data


def decode_chunk(data: object) -> bytes:
    """Decode a stdout/stderr event payload to raw bytes.

    Typed payloads delegate to :meth:`EventDataStream.as_bytes`; the
    raw dict a lenient event parse may leave behind is decoded the
    same way, and anything else becomes empty bytes instead of
    failing a live stream.
    """
    if isinstance(data, (EventDataStream, StreamRepr)):
        return data.as_bytes()
    if not isinstance(data, dict):
        return b""
    value = data.get("value", "")
    if not isinstance(value, str) or not value:
        return b""
    if str(data.get("encoding", "ascii")) == "base64":
        with suppress(binascii.Error, ValueError):
            return base64.b64decode(value)
        return b""
    return value.encode("utf-8", errors="replace")


def decode_stream(stream: StreamRepr | dict[str, Any] | None) -> str:
    """Decode a ``StreamRepr`` payload (model or raw dict) to a string."""
    if isinstance(stream, (EventDataStream, StreamRepr)):
        return stream.as_text()
    if not isinstance(stream, dict):
        return ""
    value = stream.get("value", "")
    if not isinstance(value, str) or not value:
        return ""
    if stream.get("encoding") == "base64":
        with suppress(binascii.Error, ValueError):
            return base64.b64decode(value).decode("utf-8", errors="replace")
        return ""
    return value
'''

PARSE_DATETIME_BLOCK = '''
FRACTION_RE = re.compile(r"^(.*T\\d{2}:\\d{2}:\\d{2})\\.(\\d+)(.*)$")


def parse_datetime(value: str) -> datetime:
    """Parse an ISO 8601 timestamp, tolerating Z suffix and nanoseconds."""
    v = value.strip()
    if v.endswith(("Z", "z")):
        v = v[:-1] + "+00:00"
    match = FRACTION_RE.match(v)
    if match:
        # python 3.10 fromisoformat accepts only 3- or 6-digit
        # fractions: trim nanoseconds AND zero-pad short fractions
        fraction = match.group(2)[:6].ljust(6, "0")
        v = f"{match.group(1)}.{fraction}{match.group(3)}"
    return datetime.fromisoformat(v)


def wire_value(value: Any) -> Any:
    """Recursively encode a value to its JSON-compatible wire form.

    Models, datetime and Enum are converted wherever they sit -
    directly in a field or nested inside lists and mappings.
    """
    if isinstance(value, ContreeModel):
        return value.to_dict()
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (list, tuple)):
        return [wire_value(item) for item in value]
    if isinstance(value, dict):
        return {key: wire_value(item) for key, item in value.items()}
    return value


def omitted_dict(items: list[tuple[str, Any]]) -> dict[str, Any]:
    """`dict_factory` for `dataclasses.asdict` used by `to_dict()`.

    Fields left unset (`...`) are omitted entirely so the server-side
    defaults apply, while an explicit None is serialized as JSON null;
    datetime and enum values become JSON-friendly at any nesting depth.
    """
    return {key: wire_value(value) for key, value in items if value is not Ellipsis}


TModel = TypeVar("TModel", bound="ContreeModel")


@dataclass
class ContreeModel:
    """Base model with the default wire (de)serialization.

    `to_dict` uses each field's wire name and omits unset (`...`)
    fields while keeping explicit None as JSON null.
    `from_dict` builds the model from `parse_fields`; models whose
    fields need conversion (nested models, datetimes, discriminated
    unions) override `parse_fields` only.
    """

    @classmethod
    def parse_fields(cls, data: dict[str, Any]) -> dict[str, Any]:
        return {
            item.name: data[wire_name]
            for item in fields(cls)
            if (wire_name := item.metadata.get("wire_name", item.name)) in data
        }

    @classmethod
    def from_dict(cls: type[TModel], data: dict[str, Any]) -> TModel:
        return cls(**cls.parse_fields(data))

    def to_dict(self) -> dict[str, Any]:
        return omitted_dict(
            [
                (
                    item.metadata.get("wire_name", item.name),
                    getattr(self, item.name),
                )
                for item in fields(self)
            ]
        )
'''


def used_names(source: str, names: Iterable[str]) -> list[str]:
    return [
        name
        for name in sorted(set(names))
        if re.search(rf"\b{re.escape(name)}\b", source)
    ]


def import_block(module: str, names: Iterable[str]) -> str:
    joined = "\n".join(f"{INDENT}{name}," for name in names)
    return f"from {module} import (\n{joined}\n)"


# ---------------------------------------------------------------------------
# models.py
# ---------------------------------------------------------------------------


def render_type_ref(type_ref: TypeRef) -> str:
    atoms = {
        TypeKind.ANY: "Any",
        TypeKind.STRING: "str",
        TypeKind.INTEGER: "int",
        TypeKind.NUMBER: "float",
        TypeKind.BOOLEAN: "bool",
        TypeKind.DATETIME: "datetime",
        TypeKind.BYTES: "bytes",
        TypeKind.BINARY_STREAM: "IO[bytes]",
    }
    atom = atoms.get(type_ref.kind)
    if atom is not None:
        return atom
    if type_ref.kind in (TypeKind.MODEL, TypeKind.ENUM, TypeKind.ALIAS):
        if type_ref.name is None:
            raise ValueError(f"{type_ref.kind.name} type has no name")
        return type_ref.name
    if type_ref.kind is TypeKind.LITERAL:
        return "Literal[" + ", ".join(repr(value) for value in type_ref.values) + "]"
    if type_ref.kind is TypeKind.LIST:
        return f"list[{render_type_ref(type_ref.arguments[0])}]"
    if type_ref.kind is TypeKind.SEQUENCE:
        return f"Sequence[{render_type_ref(type_ref.arguments[0])}]"
    if type_ref.kind is TypeKind.MAP:
        return f"dict[str, {render_type_ref(type_ref.arguments[0])}]"
    if type_ref.kind is TypeKind.UNION:
        return " | ".join(render_type_ref(item) for item in type_ref.arguments)
    raise ValueError(f"unsupported Python type kind: {type_ref.kind.name}")


def render_parse_value(type_ref: TypeRef, expr: str) -> str:
    if type_ref.kind is TypeKind.DATETIME:
        return f"parse_datetime({expr})"
    if type_ref.kind is TypeKind.MODEL:
        return f"{type_ref.name}.from_dict({expr})"
    if type_ref.kind is TypeKind.ENUM:
        return f"{type_ref.name}({expr})"
    if type_ref.kind is TypeKind.LIST:
        item = render_parse_value(type_ref.arguments[0], "item")
        return expr if item == "item" else f"[{item} for item in {expr}]"
    if type_ref.kind is TypeKind.MAP:
        value = render_parse_value(type_ref.arguments[0], "value")
        if value == "value":
            return expr
        return f"{{key: {value} for key, value in {expr}.items()}}"
    return expr


LINE_LIMIT = 88
# Implicit-concatenation chunks are laid out by the formatter at the
# metadata-value depth: dict items sit at column 12, chunks at 16.
METADATA_ITEM_COLUMN = 12
CHUNK_WIDTH = LINE_LIMIT - METADATA_ITEM_COLUMN - 4


def render_string_literal(value: object, offset: int = 0) -> str:
    literal = repr(value)
    if not isinstance(value, str) or offset + len(literal) <= LINE_LIMIT:
        return literal
    chunks: list[str] = []
    current = ""
    for part in re.split(r"(?<= )", value):
        if current and len(repr(current + part)) > CHUNK_WIDTH:
            chunks.append(current)
            current = part
        else:
            current += part
    if current:
        chunks.append(current)
    return "(" + " ".join(repr(chunk) for chunk in chunks) + ")"


def render_field_annotation(field_def: FieldDef) -> str:
    annotation = render_type_ref(field_def.type)
    if field_def.nullable and not annotation.endswith("| None"):
        annotation = f"{annotation} | None"
    if not field_def.required:
        annotation = f"{annotation} | EllipsisType"
    return annotation


def render_field_metadata(field_def: FieldDef) -> str:
    documentation = field_def.documentation
    items: list[str] = []
    for key, value, present in (
        (
            "wire_name",
            field_def.wire_name,
            field_def.wire_name != field_def.name,
        ),
        (
            "description",
            documentation.description,
            bool(documentation.description),
        ),
        ("example", documentation.example, documentation.has_example),
        ("default", field_def.default_value, field_def.has_default),
    ):
        if not present:
            continue
        offset = METADATA_ITEM_COLUMN + len(f'"{key}": ') + len(",")
        items.append(f'"{key}": {render_string_literal(value, offset)}')
    return "{" + ", ".join(items) + "}" if items else ""


def render_field_declaration(field_def: FieldDef) -> str:
    annotation = render_field_annotation(field_def)
    metadata = render_field_metadata(field_def)
    if not metadata:
        declaration = f"{field_def.name}: {annotation}"
        if not field_def.required:
            declaration += " = ..."
        return declaration
    arguments = [] if field_def.required else ["default=..."]
    arguments.append(f"metadata={metadata}")
    return f"{field_def.name}: {annotation} = field({', '.join(arguments)})"


def render_discriminator_parse(field_def: FieldDef, expr: str) -> str:
    discriminator = field_def.discriminator
    if discriminator is None:
        raise ValueError(f"{field_def.name} has no discriminator")
    if discriminator.name == "EventData":
        return f'parse_event_data(data["{discriminator.parent_field}"], {expr})'
    rendered = render_parse_value(discriminator.fallback, expr)
    for value, type_ref in reversed(discriminator.cases):
        parsed = render_parse_value(type_ref, expr)
        rendered = (
            f'{parsed} if data.get("{discriminator.parent_field}") == {value!r}'
            f" else {rendered}"
        )
    return rendered


def render_field_parse(field_def: FieldDef) -> str:
    source = f'data["{field_def.wire_name}"]'
    present = f'data.get("{field_def.wire_name}")'
    missing = f'data.get("{field_def.wire_name}", ...)'
    parsed = (
        render_discriminator_parse(field_def, source)
        if field_def.discriminator is not None
        else render_parse_value(field_def.type, source)
    )
    if field_def.required:
        if parsed == source:
            return source if not field_def.nullable else present
        if not field_def.nullable:
            return parsed
        return f"({parsed}) if {present} is not None else None"
    if parsed == source:
        return missing
    return f"({parsed}) if {present} is not None else {missing}"


STREAM_VALUE_METHODS = '''
    def as_bytes(self) -> bytes:
        """The payload decoded to raw bytes.

        ``base64`` values are decoded; ``ascii`` text is UTF-8
        encoded. Undecodable base64 becomes empty bytes instead of
        failing a live stream.
        """
        if not self.value:
            return b""
        if self.encoding == "base64":
            try:
                return base64.b64decode(self.value)
            except (binascii.Error, ValueError):
                return b""
        return self.value.encode("utf-8", errors="replace")

    def as_text(self) -> str:
        """The payload decoded to text (UTF-8, errors replaced)."""
        if self.encoding == "base64":
            return self.as_bytes().decode("utf-8", errors="replace")
        return self.value

    @classmethod
    def from_bytes(cls, value: bytes) -> "{name}":
        """Build a payload from raw bytes.

        Printable ASCII (plus tab/newline/carriage return) is stored
        as ``ascii``; anything else is base64-encoded - the same rule
        the server applies on its side.
        """
        if all(32 <= byte < 127 or byte in (9, 10, 13) for byte in value):
            return cls(value=value.decode("ascii"), encoding="ascii")
        return cls(
            value=base64.b64encode(value).decode("ascii"),
            encoding="base64",
        )

    @classmethod
    def from_text(cls, value: str) -> "{name}":
        """Build a payload from text (UTF-8 encoded unless ASCII)."""
        return cls.from_bytes(value.encode("utf-8"))
'''

FILESPEC_MODE_METHODS = """
    def __post_init__(self) -> None:
        # the wire format is an octal string; accept a plain int too
        if isinstance(self.mode, int):
            self.mode = f"{{self.mode:04o}}"
"""


def ordered_model_fields(model: ModelDef) -> list[FieldDef]:
    required = [field for field in model.fields if field.required]
    optional = [field for field in model.fields if not field.required]
    return required + optional


def model_needs_parse_override(model: ModelDef) -> bool:
    return any(
        field.discriminator is not None
        or render_parse_value(field.type, "value") != "value"
        for field in model.fields
    )


def render_model(model: ModelDef) -> str:
    lines: list[str] = ["@dataclass", f"class {model.name}(ContreeModel):"]
    if model.description:
        lines.extend(
            build_docstring(
                INDENT,
                model.description,
                description_body(model.description),
            )
        )
        lines.append("")
    fields = ordered_model_fields(model)
    lines.extend(f"{INDENT}{render_field_declaration(field)}" for field in fields)
    if model_needs_parse_override(model):
        lines.append("")
        lines.append(f"{INDENT}@classmethod")
        lines.append(
            f"{INDENT}def parse_fields(cls, data: dict[str, Any]) -> dict[str, Any]:"
        )
        lines.append(f"{INDENT * 2}return {{")
        lines.extend(
            f'{INDENT * 3}"{field.name}": {render_field_parse(field)},'
            for field in fields
        )
        lines.append(f"{INDENT * 2}}}")
    if ModelTrait.STREAM_VALUE in model.traits:
        lines.append(STREAM_VALUE_METHODS.format(name=model.name).rstrip("\n"))
    if ModelTrait.FILE_MODE in model.traits:
        lines.append(FILESPEC_MODE_METHODS.format(name=model.name).rstrip("\n"))
    return "\n".join(lines)


def render_event_data_block(ir: SpecIR) -> str:
    model_names: list[str] = []
    parser_lines: list[str] = []
    for event_type, type_ref in ir.event_data_variants:
        if type_ref.kind is not TypeKind.MODEL or type_ref.name is None:
            raise ValueError(f"event data variant {event_type!r} is not a model")
        if type_ref.name not in model_names:
            model_names.append(type_ref.name)
        parser_lines.append(f'    "{event_type}": {type_ref.name}.from_dict,')

    alias_lines = ["EventData = ("]
    for index, name in enumerate(model_names):
        prefix = "    " if index == 0 else "    | "
        alias_lines.append(f"{prefix}{name}")
    alias_lines.append(")")
    parser = [
        "EVENT_DATA_PARSERS: dict[str, Callable[[dict[str, Any]], EventData]] = {",
        *parser_lines,
        "}",
    ]
    return "\n\n".join(
        ("\n".join(alias_lines), "\n".join(parser), EVENT_DATA_HELPERS.strip("\n"))
    )


def render_operation_status(ir: SpecIR) -> str:
    members = "\n".join(f'{INDENT}{value} = "{value}"' for value in ir.status_values)
    terminal = ", ".join(
        f"OperationStatus.{value}" for value in ir.terminal_status_values
    )
    active = ", ".join(
        f"OperationStatus.{value}"
        for value in ir.status_values
        if value not in ir.terminal_status_values
    )
    enum_source = (
        "class OperationStatus(str, Enum):\n"
        f'{INDENT}"""Operation lifecycle state."""\n\n'
        f"{members}\n\n"
        f"{INDENT}def __str__(self) -> str:\n"
        f"{INDENT * 2}return self.value\n\n"
        f"{INDENT}def is_terminal(self) -> bool:\n"
        f'{INDENT * 2}"""True for statuses that will never change again."""\n'
        f"{INDENT * 2}return self in TERMINAL_STATUSES\n\n"
        f"{INDENT}@classmethod\n"
        f'{INDENT}def terminal(cls) -> frozenset["OperationStatus"]:\n'
        f'{INDENT * 2}"""Statuses that will never change again."""\n'
        f"{INDENT * 2}return TERMINAL_STATUSES\n\n"
        f"{INDENT}@classmethod\n"
        f'{INDENT}def active(cls) -> frozenset["OperationStatus"]:\n'
        f'{INDENT * 2}"""Statuses of operations that are still in flight."""\n'
        f"{INDENT * 2}return ACTIVE_STATUSES"
    )
    # the trailing comma matters: without it a singleton would be
    # frozenset(str) - a set of the value's CHARACTERS, str-enum
    # oblige; an empty set must render frozenset(), not frozenset((,))
    terminal_literal = f"frozenset(({terminal},))" if terminal else "frozenset()"
    active_literal = f"frozenset(({active},))" if active else "frozenset()"
    sets_source = (
        "# str-enum members compare and hash like their values, so these\n"
        '# also answer membership for plain strings ("SUCCESS" in ...)\n'
        f"TERMINAL_STATUSES = {terminal_literal}\n"
        f"ACTIVE_STATUSES = {active_literal}"
    )
    return f"{enum_source}\n\n\n{sets_source}"


def render_models(ir: SpecIR) -> str:
    event_type_literal = "\n".join(
        f"{INDENT}{value!r}," for value in ir.event_type_values
    )
    imports = "\n".join(
        [
            "import base64",
            "import binascii",
            "import re",
            "from collections.abc import Callable",
            "from contextlib import suppress",
            "from dataclasses import dataclass, field, fields",
            "from datetime import datetime, timezone",
            "from enum import Enum",
            "from types import EllipsisType",
            "from typing import Any, Literal, TypeVar",
        ]
    )
    parts = [
        f'"""Data models for the Contree API.\n\n{GENERATED_NOTE}\n"""',
        "from __future__ import annotations",
        imports,
        PARSE_DATETIME_BLOCK.strip("\n"),
        f"OperationEventType = Literal[\n{event_type_literal}\n]",
        render_operation_status(ir),
    ]
    parts.extend(render_model(model) for model in ir.models)
    parts.append(render_event_data_block(ir))
    return "\n\n\n".join(parts) + "\n"


# ---------------------------------------------------------------------------
# operations.py
# ---------------------------------------------------------------------------


def render_documentation(documentation: Documentation) -> str:
    return documentation_text(
        documentation.description,
        documentation.example,
        documentation.has_example,
    )


def render_function(
    name: str,
    signature: str,
    return_annotation: str,
    docstring: str,
    body: list[str],
) -> str:
    lines = [f"def {name}({signature}) -> {return_annotation}:"]
    if docstring:
        lines.extend(render_docstring(docstring, INDENT))
    lines.extend(f"{INDENT}{line}" if line else "" for line in body)
    return "\n".join(lines)


def render_argument_annotation(argument: ArgumentDef) -> str:
    annotation = render_type_ref(argument.type)
    if (
        argument.nullable or argument.presence is ArgumentPresence.OMIT_IF_NULL
    ) and not annotation.endswith("| None"):
        annotation = f"{annotation} | None"
    if argument.presence is ArgumentPresence.OMIT_IF_UNSET:
        annotation = f"{annotation} | EllipsisType"
    return annotation


def render_argument_default(argument: ArgumentDef) -> str | None:
    return {
        ArgumentPresence.REQUIRED: None,
        ArgumentPresence.OMIT_IF_NULL: "None",
        ArgumentPresence.OMIT_IF_FALSE: "False",
        ArgumentPresence.OMIT_IF_UNSET: "...",
    }[argument.presence]


def ordered_operation_arguments(operation: OperationDef) -> list[ArgumentDef]:
    required = [argument for argument in operation.arguments if argument.required]
    optional = [argument for argument in operation.arguments if not argument.required]
    return required + optional


def render_operation_signature(operation: OperationDef) -> str:
    arguments = ordered_operation_arguments(operation)
    required = [argument for argument in arguments if argument.required]
    optional = [argument for argument in arguments if not argument.required]
    parts = [
        f"{argument.name}: {render_argument_annotation(argument)}"
        for argument in required
    ]
    if optional:
        parts.append("*")
        parts.extend(
            f"{argument.name}: {render_argument_annotation(argument)}"
            f" = {render_argument_default(argument)}"
            for argument in optional
        )
    return ", ".join(parts)


def render_passthrough(operation: OperationDef) -> str:
    return ", ".join(
        f"{argument.name}={argument.name}" for argument in operation.arguments
    )


def argument_by_name(operation: OperationDef, name: str) -> ArgumentDef:
    for argument in operation.arguments:
        if argument.name == name:
            return argument
    raise ValueError(f"operation {operation.name!r} has no argument {name!r}")


def render_dump_value(type_ref: TypeRef, expr: str) -> str:
    if type_ref.kind is TypeKind.DATETIME:
        return f"{expr}.isoformat()"
    if type_ref.kind is TypeKind.MODEL:
        return f"{expr}.to_dict()"
    if type_ref.kind is TypeKind.ENUM:
        return f"{expr}.value"
    if type_ref.kind in (TypeKind.LIST, TypeKind.SEQUENCE):
        item = render_dump_value(type_ref.arguments[0], "item")
        return expr if item == "item" else f"[{item} for item in {expr}]"
    if type_ref.kind is TypeKind.MAP:
        value = render_dump_value(type_ref.arguments[0], "value")
        if value == "value":
            return expr
        return f"{{key: {value} for key, value in {expr}.items()}}"
    return expr


def render_parameter_value(parameter: ParameterDef) -> str:
    name = parameter.argument
    if parameter.encoding is ParameterEncoding.TIME:
        return f"format_time_param({name})"
    if parameter.encoding is ParameterEncoding.STRING:
        return f"str({name})"
    return name


def render_parameter_assignment(
    operation: OperationDef,
    parameter: ParameterDef,
    target: str,
) -> list[str]:
    argument = argument_by_name(operation, parameter.argument)
    if parameter.encoding is ParameterEncoding.ONE_IF_TRUE:
        return [
            f"if {argument.name}:",
            f'{INDENT}{target} = "1"',
        ]
    assignment = f"{target} = {render_parameter_value(parameter)}"
    if argument.required:
        return [assignment]
    if argument.presence is ArgumentPresence.OMIT_IF_UNSET:
        condition = f"{argument.name} is not ..."
    elif argument.presence is ArgumentPresence.OMIT_IF_FALSE:
        condition = argument.name
    else:
        condition = f"{argument.name} is not None"
    return [f"if {condition}:", f"{INDENT}{assignment}"]


def render_request_body(operation: OperationDef) -> list[str]:
    body = operation.request.body
    if body is None or body.kind is BodyKind.BINARY:
        return []
    if body.kind is BodyKind.JSON_MODEL:
        if body.model is None:
            raise ValueError(f"operation {operation.name!r} has no body model")
        arguments = ", ".join(
            f"{binding.argument}={binding.argument}" for binding in body.bindings
        )
        return [f"payload = {render_type_ref(body.model)}({arguments}).to_dict()"]

    lines = ["payload: dict[str, Any] = {}"]
    for binding in body.bindings:
        argument = argument_by_name(operation, binding.argument)
        dumped = render_dump_value(argument.type, argument.name)
        assignment = f'payload["{binding.wire_name}"] = {dumped}'
        if argument.required:
            lines.append(assignment)
        else:
            lines.append(f"if {argument.name} is not ...:")
            lines.append(f"{INDENT}{assignment}")
    return lines


def render_request_path(operation: OperationDef) -> str:
    path_parameters = [
        parameter
        for parameter in operation.request.parameters
        if parameter.location is ParameterLocation.PATH
    ]
    if not path_parameters:
        return f'request_path = "{operation.path}"'
    rendered = operation.path
    for parameter in path_parameters:
        rendered = rendered.replace(
            "{" + parameter.wire_name + "}",
            "{quote_path(" + parameter.argument + ")}",
        )
    return f'request_path = f"{rendered}"'


def render_request_builder(operation: OperationDef) -> str:
    query = [
        parameter
        for parameter in operation.request.parameters
        if parameter.location is ParameterLocation.QUERY
    ]
    headers = [
        parameter
        for parameter in operation.request.parameters
        if parameter.location is ParameterLocation.HEADER
    ]
    body: list[str] = []
    if query:
        query_type = (
            "str | Sequence[str]"
            if any(parameter.repeatable for parameter in query)
            else "str"
        )
        body.append(f"query: dict[str, {query_type}] = {{}}")
        for parameter in query:
            body.extend(
                render_parameter_assignment(
                    operation,
                    parameter,
                    f'query["{parameter.wire_name}"]',
                )
            )
    if headers:
        body.append("headers: dict[str, str] = {}")
        for parameter in headers:
            body.extend(
                render_parameter_assignment(
                    operation,
                    parameter,
                    f'headers["{parameter.wire_name}"]',
                )
            )
    body.extend(render_request_body(operation))
    body.append(render_request_path(operation))

    request_body = operation.request.body
    spec_args = [
        f'method="{operation.http_method}"',
        "path=request_path",
        f"idempotent={operation.request.idempotent}",
    ]
    if query:
        spec_args.append("query=query")
    if headers:
        spec_args.append("headers=headers")
    if request_body is not None:
        if request_body.kind in (BodyKind.JSON_MODEL, BodyKind.JSON_INLINE):
            spec_args.append("body=json.dumps(payload).encode()")
            spec_args.append('content_type="application/json"')
        elif request_body.kind is BodyKind.BINARY:
            binding = request_body.bindings[0]
            spec_args.append(f"body={binding.argument}")
            spec_args.append('content_type="application/octet-stream"')
    if operation.request.accept is not None:
        spec_args.append(f'accept="{operation.request.accept}"')
    body.append(f"return RequestSpec({', '.join(spec_args)})")

    return render_function(
        f"build_{operation.name}",
        render_operation_signature(operation),
        "RequestSpec",
        f"Build the request for `{operation.http_method} {operation.path}`.",
        body,
    )


def render_success_condition(operation: OperationDef) -> str:
    response = operation.response
    if response.success is SuccessPolicy.ANY_2XX:
        return "200 <= response.status < 300"
    if len(response.success_statuses) == 1:
        return f"response.status == {response.success_statuses[0]}"
    statuses = ", ".join(str(status) for status in response.success_statuses)
    return f"response.status in ({statuses})"


def render_json_response(operation: OperationDef) -> list[str]:
    response_type = operation.response.type
    if response_type is None:
        raise ValueError(f"operation {operation.name!r} has no JSON response type")
    condition = render_success_condition(operation)
    if operation.response.json_path:
        value = "json_object(response)"
        for part in operation.response.json_path:
            value += f"[{part!r}]"
        if response_type.kind is TypeKind.STRING:
            value = f"str({value})"
        elif response_type.kind is TypeKind.INTEGER:
            value = f"int({value})"
        else:
            value = render_parse_value(response_type, value)
        return [f"if {condition}:", f"{INDENT}return {value}"]
    if response_type.kind is TypeKind.MODEL:
        return [
            f"if {condition}:",
            f"{INDENT}return {response_type.name}.from_dict(json_object(response))",
        ]
    if response_type.kind is TypeKind.LIST:
        item_type = response_type.arguments[0]
        parsed = render_parse_value(item_type, "item")
        return [
            f"if {condition}:",
            f"{INDENT}return [",
            f"{INDENT * 2}{parsed} for item in json_array(response)",
            f"{INDENT}]",
        ]
    parsed = render_parse_value(response_type, "json_object(response)")
    return [f"if {condition}:", f"{INDENT}return {parsed}"]


def render_response_parser(operation: OperationDef) -> str | None:
    response = operation.response
    if response.mode in (ResponseMode.SSE, ResponseMode.BYTE_STREAM):
        return None
    condition = render_success_condition(operation)
    body: list[str]
    if response.mode is ResponseMode.JSON:
        body = render_json_response(operation)
    elif response.mode is ResponseMode.LOCATION:
        if response.header_name is None:
            raise ValueError(f"operation {operation.name!r} has no location header")
        body = [
            f"if {condition}:",
            f'{INDENT}location = response.headers["{response.header_name}"]',
            f'{INDENT}return location.rstrip("/").rsplit("/", 1)[-1]',
        ]
    elif response.mode is ResponseMode.STATUS_BOOL:
        body = [f"if {condition}:", f"{INDENT}return True"]
        for status in response.false_statuses:
            body.extend([f"if response.status == {status}:", f"{INDENT}return False"])
    elif response.mode is ResponseMode.BYTES:
        body = [f"if {condition}:", f"{INDENT}return response.body"]
    elif response.mode is ResponseMode.EMPTY:
        body = [f"if {condition}:", f"{INDENT}return None"]
    else:
        raise ValueError(response.mode)
    body.append('raise ValueError(f"unexpected HTTP status {response.status}")')
    return render_function(
        f"parse_{operation.name}",
        "response: ResponseData",
        render_operation_return_type(operation),
        f"Parse the response of `{operation.http_method} {operation.path}`.",
        body,
    )


def render_operation_return_type(operation: OperationDef) -> str:
    if operation.response.mode is ResponseMode.EMPTY:
        return "None"
    if operation.response.type is None:
        return "None"
    return render_type_ref(operation.response.type)


def render_operations(ir: SpecIR) -> str:
    bodies: list[str] = []
    for operation in ir.operations:
        bodies.append(render_request_builder(operation))
        parser = render_response_parser(operation)
        if parser is not None:
            bodies.append(parser)
    source = "\n\n\n".join(bodies)
    model_names = used_names(source, [*ir.model_names, "OperationStatus"])
    imports = "\n".join(
        [
            "import json",
            "from collections.abc import Sequence",
            "from datetime import datetime, timezone",
            "from types import EllipsisType",
            "from typing import IO, Any, Literal",
            "",
            import_block(".models", model_names),
            import_block(
                ".runtime",
                [
                    "RequestSpec",
                    "ResponseData",
                    "format_time_param",
                    "json_array",
                    "json_object",
                    "quote_path",
                ],
            ),
        ]
    )
    parts = [
        (
            '"""Request builders and response parsers for the Contree'
            f' API.\n\n{GENERATED_NOTE}\n"""'
        ),
        "from __future__ import annotations",
        imports,
        source,
    ]
    return "\n\n\n".join(parts) + "\n"


# ---------------------------------------------------------------------------
# base.py
# ---------------------------------------------------------------------------

BASE_HEADER = '''"""Base client classes with the full generated API surface.

{note}
"""

# E501 only: spec-provided docstrings embed markdown tables that
# cannot be wrapped without breaking them; everything else in this
# file obeys the line limit and stays lint-gated
# ruff: noqa: E501

from __future__ import annotations

import asyncio
import logging
import platform
import sys
import time
from abc import ABC, abstractmethod
from collections.abc import AsyncGenerator, Iterable, Iterator, Sequence
from contextlib import aclosing
from datetime import datetime
from pathlib import Path
from types import EllipsisType, TracebackType
from typing import IO, Any, Literal, TypeVar
from urllib.parse import urlsplit

from . import operations
from .exceptions import (
    APIConnectionError,
    APIStatusError,
    NotFoundError,
)
{model_imports}
from .profiles import AUTH_TYPE_IAM, Profile, ProfileError, resolve_profile
from .runtime import (
    REQUEST_DEADLINE_MESSAGE,
    TIGHT_LOOP_FLOOR,
    BodyFormatter,
    HeaderFormatter,
    RequestSpec,
    ResponseData,
    RetryPolicy,
    SSEParser,
    body_start,
    decode_event_frame,
    encode_query,
    file_sha256,
    is_uuid,
    logger,
    package_version,
    retry_generator,
    rewind_body,
)
from .spec_info import DEFAULT_BASE_URL

TClient = TypeVar("TClient", bound="ContreeClientBase")
TSyncClient = TypeVar("TSyncClient", bound="ContreeSyncClient")
TAsyncClient = TypeVar("TAsyncClient", bound="ContreeAsyncClient")


def deadline_limits_timeout(
    deadline: float | None,
    timeout: float | None,
) -> bool:
    return deadline is not None and (
        timeout is None or deadline - time.monotonic() <= timeout
    )


def deadline_due(
    deadline: float | None,
    timeout_limited: bool = False,
) -> bool:
    return deadline is not None and (
        timeout_limited or time.monotonic() >= deadline
    )


class ContreeClientBase:
    """Shared configuration, URL and header building.

    Implementations replace ``log`` with a child logger named after
    the backend; raw request/response logging happens here so every
    transport reports uniformly.
    """

    log: logging.Logger = logger
    # User-Agent product tokens; adapters override UA_TRANSPORT_LIBRARY
    UA_PRODUCT = f"contree-client/{{package_version()}}"
    UA_TRANSPORT_LIBRARY = ""
    UA_PYTHON_VERSION = f"Python/{{'.'.join(map(str, sys.version_info[:3]))}}"
    UA_PLATFORM = platform.platform()

    def __init__(
        self,
        token: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        project: str | None = None,
        timeout: float | None = 300.0,
        retry: RetryPolicy | None = None,
        identity: str | None = None,
    ) -> None:
        # a typo like "htps://" must not silently degrade to plaintext
        # HTTP on port 80 with the bearer token in the clear
        parts = urlsplit(base_url)
        if parts.scheme not in ("http", "https"):
            raise ValueError(
                f"unsupported base_url scheme {{parts.scheme!r}}"
                f" in {{base_url!r}}: use http:// or https://"
            )
        # Reject permanent URL syntax errors before a backend can
        # misclassify them as transient transport failures.
        hostname = parts.hostname
        if not hostname:
            raise ValueError(f"base_url has no hostname: {{base_url!r}}")
        if any(
            character.isspace() or ord(character) < 32 or ord(character) == 127
            for character in hostname
        ):
            raise ValueError(f"base_url has invalid hostname: {{base_url!r}}")
        try:
            _ = parts.port
        except ValueError as exc:
            raise ValueError(f"base_url has invalid port: {{base_url!r}}") from exc
        self.token = token
        self.base_url = base_url.rstrip("/")
        self.project = project
        self.timeout = timeout
        self.retry = retry
        # an application product token prepended to the User-Agent,
        # e.g. identity="my-app/1.2.3" - the library tokens stay
        self.identity = identity

    @classmethod
    def from_profile(
        cls: type[TClient],
        profile: str | Profile | None = None,
        *,
        config_path: str | Path | None = None,
        **kwargs: Any,
    ) -> TClient:
        """Create a client from a saved Contree profile.

        Resolution order: explicit *profile* argument, then the
        ``CONTREE_PROFILE`` environment variable, then the active
        profile recorded in the config file.
        """
        resolved = (
            profile
            if isinstance(profile, Profile)
            else resolve_profile(profile, path=config_path)
        )
        if resolved.token is None or not resolved.token.strip():
            raise ProfileError(f"profile {{resolved.name!r}} has no token")
        if not resolved.url and resolved.auth_type != AUTH_TYPE_IAM:
            raise ProfileError(f"profile {{resolved.name!r}} has no URL")
        return cls(
            resolved.token,
            base_url=resolved.url or DEFAULT_BASE_URL,
            project=resolved.project,
            **kwargs,
        )

    def build_url(self, spec: RequestSpec) -> str:
        url = f"{{self.base_url}}/v1{{spec.path}}"
        if spec.query:
            url = f"{{url}}?{{encode_query(spec.query)}}"
        return url

    def build_headers(self, spec: RequestSpec) -> Iterable[tuple[str, str]]:
        """Build the request headers as an ordered iterable of pairs.

        Pairs, not a mapping: RFC 9110 allows repeated field names,
        which a dict cannot represent. Transports whose libraries only
        accept mappings collapse it themselves.
        """
        headers = [("Authorization", f"Bearer {{self.token}}")]
        if self.project is not None:
            headers.append(("Project", self.project))
        if spec.content_type is not None:
            headers.append(("Content-Type", spec.content_type))
        if spec.accept is not None:
            headers.append(("Accept", spec.accept))
        headers.extend(spec.headers.items())
        if all(key.lower() != "user-agent" for key, unused in headers):
            headers.append(("User-Agent", self.user_agent()))
        return headers

    def user_agent(self) -> str:
        """Compose the User-Agent from the ``UA_*`` product tokens.

        The caller's ``identity`` (constructor kwarg) leads, so an
        application announces itself without erasing the library and
        transport tokens.
        """
        parts = (
            self.identity or "",
            self.UA_PRODUCT,
            self.UA_TRANSPORT_LIBRARY,
            self.UA_PYTHON_VERSION,
            self.UA_PLATFORM,
        )
        return " ".join(part for part in parts if part)

    def log_request(self, spec: RequestSpec) -> None:
        """Log the raw outgoing request; secrets are redacted."""
        if not self.log.isEnabledFor(logging.DEBUG):
            return
        self.log.debug(
            "%s %s headers=%s body=%s",
            spec.method,
            self.build_url(spec),
            HeaderFormatter(self.build_headers(spec)),
            BodyFormatter(spec.body, spec.content_type or ""),
        )

    def log_response(self, spec: RequestSpec, response: ResponseData) -> None:
        """Log the raw buffered response."""
        if not self.log.isEnabledFor(logging.DEBUG):
            return
        self.log.debug(
            "%s %s -> %d headers=%s body=%s",
            spec.method,
            self.build_url(spec),
            response.status,
            HeaderFormatter(response.headers.items()),
            BodyFormatter(
                response.body,
                response.headers.get("content-type", ""),
            ),
        )
'''

SYNC_CLASS_HEADER = '''class ContreeSyncClient(ContreeClientBase, ABC):
    """Synchronous Contree API client interface.

    Backend implementations only provide :meth:`request`,
    :meth:`stream` and :meth:`close`.
    """

    @abstractmethod
    def request(self, spec: RequestSpec) -> ResponseData:
        """Execute the request and return the buffered response."""

    @abstractmethod
    def stream(
        self,
        spec: RequestSpec,
        auto_decompress: bool = True,
    ) -> Iterator[bytes]:
        """Execute the request and yield response body chunks.

        With ``auto_decompress=False`` the body is yielded exactly as
        served (e.g. the gzip the server always applies is kept).
        """

    @abstractmethod
    def close(self) -> None:
        """Release the underlying transport resources."""

    def open(self) -> None:
        """Eagerly initialize the underlying transport.

        Called by ``__enter__``. Backends that create their resources
        lazily override this; the default is a no-op.
        """

    def __enter__(self: TSyncClient) -> TSyncClient:
        self.open()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def call(self, spec: RequestSpec) -> ResponseData:
        """Execute a buffered request, retrying per the client policy.

        Without a policy this is a transparent single :meth:`request`.
        Streaming requests are never routed here - SSE consumers
        reconnect with ``Last-Event-Id`` instead.
        """
        policy = self.retry
        if policy is None:
            if spec.deadline is not None and time.monotonic() >= spec.deadline:
                raise TimeoutError(REQUEST_DEADLINE_MESSAGE)
            deadline_limited = deadline_limits_timeout(spec.deadline, self.timeout)
            try:
                response = self.request(spec)
            except APIConnectionError as exc:
                if deadline_due(spec.deadline, deadline_limited and exc.timed_out):
                    raise TimeoutError(REQUEST_DEADLINE_MESSAGE) from exc
                raise
            if spec.deadline is not None and time.monotonic() >= spec.deadline:
                raise TimeoutError(REQUEST_DEADLINE_MESSAGE)
            return response
        # a lost response after a non-idempotent request (POST) could
        # mean a second execution server-side: never blind-retry
        # unless the caller explicitly opted into that risk. 425 Too
        # Early and 429 Too Many Requests are the exceptions - the
        # backend's contract guarantees both mean the request was
        # rejected before any processing, so replaying is always safe.
        replay_safe = spec.idempotent or policy.retry_unsafe
        delays = retry_generator(policy.delays)
        # a retry must replay exactly the bytes the first attempt sent
        start = body_start(spec)
        attempts = 0
        while True:
            if spec.deadline is not None and time.monotonic() >= spec.deadline:
                raise TimeoutError(REQUEST_DEADLINE_MESSAGE)
            attempts += 1
            exhausted = (
                policy.max_attempts is not None and attempts >= policy.max_attempts
            )
            deadline_limited = deadline_limits_timeout(spec.deadline, self.timeout)
            try:
                response = self.request(spec)
            except APIConnectionError as exc:
                if deadline_due(spec.deadline, deadline_limited and exc.timed_out):
                    raise TimeoutError(REQUEST_DEADLINE_MESSAGE) from exc
                if not replay_safe or exhausted:
                    raise
                delay = next(delays)
                self.log.warning(
                    "network error (%s), retrying in %.1fs...",
                    type(exc).__name__,
                    delay,
                )
                if spec.deadline is not None:
                    delay = min(
                        delay,
                        max(0.0, spec.deadline - time.monotonic()),
                    )
                time.sleep(delay)
                rewind_body(spec, start)
                continue
            except APIStatusError as exc:
                if not policy.retryable_status(exc.status) or exhausted:
                    raise
                if not replay_safe and exc.status not in (425, 429):
                    raise
                delay = exc.retry_after
                if delay is None:
                    delay = next(delays)
                self.log.warning(
                    "server answered %d, retrying in %.1fs...",
                    exc.status,
                    delay,
                )
                if spec.deadline is not None:
                    delay = min(
                        delay,
                        max(0.0, spec.deadline - time.monotonic()),
                    )
                time.sleep(delay)
                rewind_body(spec, start)
                continue
            if spec.deadline is not None and time.monotonic() >= spec.deadline:
                raise TimeoutError(REQUEST_DEADLINE_MESSAGE)
            return response

    def operation_terminal(
        self,
        operation_id: str,
        deadline: float | None = None,
    ) -> bool:
        """Best-effort check that the operation reached a terminal state."""
        spec = operations.build_get_operation_status(operation_id=operation_id)
        spec.deadline = deadline
        deadline_limited = deadline_limits_timeout(deadline, self.timeout)
        try:
            self.log_request(spec)
            response = self.request(spec)
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError(
                    f"operation {operation_id} status probe exceeded its deadline"
                )
            self.log_response(spec, response)
            status = operations.parse_get_operation_status(response).status
        except APIConnectionError as exc:
            if deadline_due(deadline, deadline_limited and exc.timed_out):
                raise TimeoutError(
                    f"operation {operation_id} status probe exceeded its deadline"
                ) from exc
            return False
        except APIStatusError as exc:
            if deadline_due(deadline):
                raise TimeoutError(
                    f"operation {operation_id} status probe exceeded its deadline"
                ) from exc
            if exc.status in (410, 425, 429) or exc.status >= 500:
                return False
            raise
        except TimeoutError as exc:
            if deadline is not None:
                raise TimeoutError(
                    f"operation {operation_id} status probe exceeded its deadline"
                ) from exc
            raise
        return not isinstance(status, EllipsisType) and status.is_terminal()

    def wait_operation(
        self,
        operation_id: str,
        *,
        timeout: float | None = None,
    ) -> OperationResponse:
        """Wait until the operation finishes, driven by its event stream.

        Follows the SSE event log (push, no polling) until the
        ``completion`` event, then fetches and returns the terminal
        ``OperationResponse``. Raises :class:`TimeoutError` after
        *timeout* expires. A sync buffered response can delay the
        exception until that response ends.
        """
        deadline = None if timeout is None else time.monotonic() + timeout
        timeout_message = f"operation {operation_id} did not complete within {timeout}s"
        try:
            for event in self.follow_operation_events(operation_id, timeout=timeout):
                self.log.debug("wait_operation: event %s %s", event.id, event.type)
        except TimeoutError as exc:
            if deadline is not None:
                raise TimeoutError(timeout_message) from exc
            raise
        spec = operations.build_get_operation_status(operation_id=operation_id)
        spec.deadline = deadline
        self.log_request(spec)
        try:
            response = self.call(spec)
        except TimeoutError as exc:
            if deadline is not None:
                raise TimeoutError(timeout_message) from exc
            raise
        self.log_response(spec, response)
        return operations.parse_get_operation_status(response)

    def follow_operation_events(
        self,
        operation_id: str,
        *,
        last_event_id: int | None = None,
        spid: int | None = None,
        since: int | None = None,
        timeout: float | None = None,
    ) -> Iterator[OperationEvent]:
        """Stream operation events with transparent reconnection.

        Native stream failures trigger a terminal-status probe and a
        reconnect from the last event id. Iteration ends at the
        ``completion`` event, a terminal status, or the timeout.
        """
        last_id = last_event_id
        deadline = None if timeout is None else time.monotonic() + timeout

        def check_deadline() -> None:
            if deadline_due(deadline):
                raise TimeoutError(
                    f"operation {operation_id} events did not complete"
                    f" within {timeout}s"
                )

        while True:
            check_deadline()
            events_before = last_id
            try:
                for event in self.iter_operation_events(
                    operation_id,
                    follow=True,
                    spid=spid,
                    since=since,
                    last_event_id=last_id,
                    deadline=deadline,
                ):
                    last_id = event.id
                    yield event
                    if event.type == "completion":
                        return
                    check_deadline()
            except Exception as exc:
                check_deadline()
                resume_id = getattr(exc, "last_event_id", None)
                if isinstance(resume_id, int):
                    last_id = resume_id
                self.log.warning("stream broken (last_id=%s): %s", last_id, exc)
            # the stream ended or broke without a completion frame:
            # the retry must not outlive the operation itself
            if self.operation_terminal(operation_id, deadline):
                return
            if last_id == events_before:
                delay = TIGHT_LOOP_FLOOR
                if deadline is not None:
                    delay = min(delay, max(0.0, deadline - time.monotonic()))
                time.sleep(delay)

    def resolve_image(self, ref: str) -> str:
        """Resolve an image reference to a UUID.

        Accepts a raw UUID, ``tag:NAME`` or a bare tag name.
        """
        if ref.startswith("tag:"):
            return self.inspect_find_image_by_tag(ref[4:])
        if is_uuid(ref):
            return ref
        return self.inspect_find_image_by_tag(ref)

    def ensure_file(
        self,
        content: bytes | IO[bytes],
        *,
        sha256: str | None = None,
    ) -> FileResponse | File:
        """Upload *content* unless the server already stores it.

        The digest (*sha256* when the caller already knows it,
        computed locally otherwise) is probed via :meth:`get_file`;
        only a miss uploads. Returns the stored file record either way
        (both shapes carry ``uuid``, ``sha256`` and ``size``).
        Non-seekable streams cannot be hashed and rewound, so without
        a caller-provided *sha256* they skip deduplication and upload
        directly.
        """
        digest = sha256 if sha256 is not None else file_sha256(content)
        if digest is None:
            return self.upload_file(content)
        try:
            return self.get_file(digest)
        except NotFoundError:
            return self.upload_file(content)
'''

ASYNC_CLASS_HEADER = '''class ContreeAsyncClient(ContreeClientBase, ABC):
    """Asynchronous Contree API client interface.

    Backend implementations only provide :meth:`request`,
    :meth:`stream` and :meth:`close`.
    """

    @abstractmethod
    async def request(self, spec: RequestSpec) -> ResponseData:
        """Execute the request and return the buffered response."""

    @abstractmethod
    def stream(
        self,
        spec: RequestSpec,
        auto_decompress: bool = True,
    ) -> AsyncGenerator[bytes, None]:
        """Execute the request and yield response body chunks.

        With ``auto_decompress=False`` the body is yielded exactly as
        served (e.g. the gzip the server always applies is kept).
        """

    @abstractmethod
    async def close(self) -> None:
        """Release the underlying transport resources."""

    async def open(self) -> None:
        """Eagerly initialize the underlying transport.

        Called by ``__aenter__`` from inside the running event loop,
        so backends can create loop-bound resources here (for example
        the ``aiohttp.ClientSession``). The default is a no-op.
        """

    async def __aenter__(self: TAsyncClient) -> TAsyncClient:
        await self.open()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.close()

    async def call(self, spec: RequestSpec) -> ResponseData:
        """Execute a buffered request, retrying per the client policy.

        Without a policy this is a transparent single :meth:`request`.
        Streaming requests are never routed here - SSE consumers
        reconnect with ``Last-Event-Id`` instead.
        """
        policy = self.retry
        if policy is None:
            if spec.deadline is not None:
                remaining = spec.deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(REQUEST_DEADLINE_MESSAGE)
            deadline_limited = deadline_limits_timeout(spec.deadline, self.timeout)
            try:
                if spec.deadline is None:
                    response = await self.request(spec)
                else:
                    response = await asyncio.wait_for(
                        self.request(spec), timeout=remaining
                    )
            except APIConnectionError as exc:
                if deadline_due(spec.deadline, deadline_limited and exc.timed_out):
                    raise TimeoutError(REQUEST_DEADLINE_MESSAGE) from exc
                raise
            except (asyncio.TimeoutError, TimeoutError) as exc:
                if spec.deadline is not None:
                    raise TimeoutError(REQUEST_DEADLINE_MESSAGE) from exc
                raise
            if spec.deadline is not None and time.monotonic() >= spec.deadline:
                raise TimeoutError(REQUEST_DEADLINE_MESSAGE)
            return response
        # a lost response after a non-idempotent request (POST) could
        # mean a second execution server-side: never blind-retry
        # unless the caller explicitly opted into that risk. 425 Too
        # Early and 429 Too Many Requests are the exceptions - the
        # backend's contract guarantees both mean the request was
        # rejected before any processing, so replaying is always safe.
        replay_safe = spec.idempotent or policy.retry_unsafe
        delays = retry_generator(policy.delays)
        # a retry must replay exactly the bytes the first attempt sent
        start = body_start(spec)
        attempts = 0
        while True:
            if spec.deadline is not None:
                remaining = spec.deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(REQUEST_DEADLINE_MESSAGE)
            attempts += 1
            exhausted = (
                policy.max_attempts is not None and attempts >= policy.max_attempts
            )
            deadline_limited = deadline_limits_timeout(spec.deadline, self.timeout)
            try:
                if spec.deadline is None:
                    response = await self.request(spec)
                else:
                    response = await asyncio.wait_for(
                        self.request(spec), timeout=remaining
                    )
            except APIConnectionError as exc:
                if deadline_due(spec.deadline, deadline_limited and exc.timed_out):
                    raise TimeoutError(REQUEST_DEADLINE_MESSAGE) from exc
                if not replay_safe or exhausted:
                    raise
                delay = next(delays)
                self.log.warning(
                    "network error (%s), retrying in %.1fs...",
                    type(exc).__name__,
                    delay,
                )
                if spec.deadline is not None:
                    delay = min(
                        delay,
                        max(0.0, spec.deadline - time.monotonic()),
                    )
                await asyncio.sleep(delay)
                rewind_body(spec, start)
                continue
            except APIStatusError as exc:
                if not policy.retryable_status(exc.status) or exhausted:
                    raise
                if not replay_safe and exc.status not in (425, 429):
                    raise
                delay = exc.retry_after
                if delay is None:
                    delay = next(delays)
                self.log.warning(
                    "server answered %d, retrying in %.1fs...",
                    exc.status,
                    delay,
                )
                if spec.deadline is not None:
                    delay = min(
                        delay,
                        max(0.0, spec.deadline - time.monotonic()),
                    )
                await asyncio.sleep(delay)
                rewind_body(spec, start)
                continue
            except (asyncio.TimeoutError, TimeoutError) as exc:
                if spec.deadline is not None:
                    raise TimeoutError(REQUEST_DEADLINE_MESSAGE) from exc
                raise
            if spec.deadline is not None and time.monotonic() >= spec.deadline:
                raise TimeoutError(REQUEST_DEADLINE_MESSAGE)
            return response

    async def operation_terminal(
        self,
        operation_id: str,
        deadline: float | None = None,
    ) -> bool:
        """Best-effort check that the operation reached a terminal state."""
        spec = operations.build_get_operation_status(operation_id=operation_id)
        spec.deadline = deadline
        deadline_limited = deadline_limits_timeout(deadline, self.timeout)
        try:
            self.log_request(spec)
            if deadline is None:
                response = await self.request(spec)
            else:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(
                        f"operation {operation_id} status probe exceeded its deadline"
                    )
                response = await asyncio.wait_for(
                    self.request(spec), timeout=remaining
                )
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError(
                    f"operation {operation_id} status probe exceeded its deadline"
                )
            self.log_response(spec, response)
            status = operations.parse_get_operation_status(response).status
        except APIConnectionError as exc:
            if deadline_due(deadline, deadline_limited and exc.timed_out):
                raise TimeoutError(
                    f"operation {operation_id} status probe exceeded its deadline"
                ) from exc
            return False
        except APIStatusError as exc:
            if deadline_due(deadline):
                raise TimeoutError(
                    f"operation {operation_id} status probe exceeded its deadline"
                ) from exc
            if exc.status in (410, 425, 429) or exc.status >= 500:
                return False
            raise
        except (asyncio.TimeoutError, TimeoutError) as exc:
            if deadline is not None:
                raise TimeoutError(
                    f"operation {operation_id} status probe exceeded its deadline"
                ) from exc
            raise
        return not isinstance(status, EllipsisType) and status.is_terminal()

    async def wait_operation(
        self,
        operation_id: str,
        *,
        timeout: float | None = None,
    ) -> OperationResponse:
        """Wait until the operation finishes, driven by its event stream.

        Follows the SSE event log (push, no polling) until the
        ``completion`` event, then fetches and returns the terminal
        ``OperationResponse``. Raises :class:`TimeoutError` when
        *timeout* seconds elapse first.
        """
        deadline = None if timeout is None else time.monotonic() + timeout
        timeout_message = f"operation {operation_id} did not complete within {timeout}s"
        try:
            async for event in self.follow_operation_events(
                operation_id, timeout=timeout
            ):
                self.log.debug("wait_operation: event %s %s", event.id, event.type)
        except (asyncio.TimeoutError, TimeoutError) as exc:
            if deadline is not None:
                raise TimeoutError(timeout_message) from exc
            raise
        spec = operations.build_get_operation_status(operation_id=operation_id)
        spec.deadline = deadline
        self.log_request(spec)
        try:
            response = await self.call(spec)
        except (asyncio.TimeoutError, TimeoutError) as exc:
            if deadline is not None:
                raise TimeoutError(timeout_message) from exc
            raise
        self.log_response(spec, response)
        return operations.parse_get_operation_status(response)

    async def follow_operation_events(
        self,
        operation_id: str,
        *,
        last_event_id: int | None = None,
        spid: int | None = None,
        since: int | None = None,
        timeout: float | None = None,
    ) -> AsyncGenerator[OperationEvent, None]:
        """Stream operation events with transparent reconnection.

        Native stream failures trigger a terminal-status probe and a
        reconnect from the last event id. Iteration ends at the
        ``completion`` event, a terminal status, or the timeout.
        """
        last_id = last_event_id
        deadline = None if timeout is None else time.monotonic() + timeout

        def check_deadline() -> None:
            if deadline_due(deadline):
                raise TimeoutError(
                    f"operation {operation_id} events did not complete"
                    f" within {timeout}s"
                )

        while True:
            check_deadline()
            events_before = last_id
            try:
                # aclosing: leaving this scope must close the transport
                # stream even when the caller aborts the iteration
                async with aclosing(
                    self.iter_operation_events(
                        operation_id,
                        follow=True,
                        spid=spid,
                        since=since,
                        last_event_id=last_id,
                        deadline=deadline,
                    )
                ) as source:
                    async for event in source:
                        last_id = event.id
                        yield event
                        if event.type == "completion":
                            return
                        check_deadline()
            except Exception as exc:
                check_deadline()
                resume_id = getattr(exc, "last_event_id", None)
                if isinstance(resume_id, int):
                    last_id = resume_id
                self.log.warning("stream broken (last_id=%s): %s", last_id, exc)
            # the stream ended or broke without a completion frame:
            # the retry must not outlive the operation itself
            if await self.operation_terminal(operation_id, deadline):
                return
            if last_id == events_before:
                delay = TIGHT_LOOP_FLOOR
                if deadline is not None:
                    delay = min(delay, max(0.0, deadline - time.monotonic()))
                await asyncio.sleep(delay)

    async def resolve_image(self, ref: str) -> str:
        """Resolve an image reference to a UUID.

        Accepts a raw UUID, ``tag:NAME`` or a bare tag name.
        """
        if ref.startswith("tag:"):
            return await self.inspect_find_image_by_tag(ref[4:])
        if is_uuid(ref):
            return ref
        return await self.inspect_find_image_by_tag(ref)

    async def ensure_file(
        self,
        content: bytes | IO[bytes],
        *,
        sha256: str | None = None,
    ) -> FileResponse | File:
        """Upload *content* unless the server already stores it.

        The digest (*sha256* when the caller already knows it,
        computed locally in a worker thread otherwise, so a large file
        does not block the event loop) is probed via :meth:`get_file`;
        only a miss uploads. Returns the stored file record either way
        (both shapes carry ``uuid``, ``sha256`` and ``size``).
        Non-seekable streams cannot be hashed and rewound, so without
        a caller-provided *sha256* they skip deduplication and upload
        directly.
        """
        if sha256 is not None:
            digest: str | None = sha256
        else:
            digest = await asyncio.to_thread(file_sha256, content)
        if digest is None:
            return await self.upload_file(content)
        try:
            return await self.get_file(digest)
        except NotFoundError:
            return await self.upload_file(content)
'''


def method_signature(operation: OperationDef) -> str:
    signature = render_operation_signature(operation)
    if signature:
        return f"self, {signature}"
    return "self"


def op_docstring(
    operation: OperationDef,
    summary: str | None = None,
    extra_entries: list[tuple[str, str]] | None = None,
) -> list[str]:
    """Docstring for a client method: spec summary, description, Args."""
    entries = [
        (argument.name, text)
        for argument in operation.arguments
        if (text := render_documentation(argument.documentation))
    ]
    entries.extend(extra_entries or [])
    return build_docstring(
        INDENT,
        summary or operation.summary or operation.name,
        operation.description,
        "Args",
        entries,
    )


def emit_call_method(operation: OperationDef, async_mode: bool) -> list[str]:
    prefix = "async " if async_mode else ""
    awaited = "await self.call(spec)" if async_mode else "self.call(spec)"
    lines = [
        f"{prefix}def {operation.name}({method_signature(operation)})"
        f" -> {render_operation_return_type(operation)}:",
    ]
    lines.extend(op_docstring(operation))
    lines.append(
        f"{INDENT}spec = operations.build_{operation.name}("
        f"{render_passthrough(operation)})"
    )
    lines.append(f"{INDENT}self.log_request(spec)")
    returns_false_on_not_found = (
        operation.response.mode is ResponseMode.STATUS_BOOL
        and 404 in operation.response.false_statuses
    )
    if returns_false_on_not_found:
        lines.append(f"{INDENT}try:")
        lines.append(f"{INDENT * 2}response = {awaited}")
        lines.append(f"{INDENT}except NotFoundError:")
        lines.append(f"{INDENT * 2}return False")
    else:
        lines.append(f"{INDENT}response = {awaited}")
    lines.append(f"{INDENT}self.log_response(spec, response)")
    lines.append(f"{INDENT}return operations.parse_{operation.name}(response)")
    return lines


def emit_stream_method(operation: OperationDef, async_mode: bool) -> list[str]:
    prefix = "async " if async_mode else ""
    iterator = "AsyncGenerator[bytes, None]" if async_mode else "Iterator[bytes]"
    lines = [
        f"{prefix}def {operation.name}_stream("
        f"{method_signature(operation)}) -> {iterator}:",
    ]
    lines.extend(
        op_docstring(
            operation,
            summary=f"Streaming variant of `{operation.name}()`.",
        )
    )
    lines.append(
        f"{INDENT}spec = operations.build_{operation.name}("
        f"{render_passthrough(operation)})"
    )
    lines.append(f"{INDENT}self.log_request(spec)")
    if async_mode:
        # aclosing: an early aclose() of this generator must close the
        # transport stream too (async finalization is not deterministic)
        lines.append(f"{INDENT}async with aclosing(self.stream(spec)) as source:")
        lines.append(f"{INDENT * 2}async for chunk in source:")
        body_indent = INDENT * 3
    else:
        lines.append(f"{INDENT}for chunk in self.stream(spec):")
        body_indent = INDENT * 2
    lines.append(f'{body_indent}self.log.debug("stream chunk: %d bytes", len(chunk))')
    lines.append(f"{body_indent}yield chunk")
    return lines


COMPRESSED_ARG_DOC = (
    "disable transparent decompression: the body is yielded exactly"
    " as served - a tar.gz stream when the server compresses the"
    " response, a plain tar otherwise."
)


def emit_stream_only_method(operation: OperationDef, async_mode: bool) -> list[str]:
    prefix = "async " if async_mode else ""
    iterator = "AsyncGenerator[bytes, None]" if async_mode else "Iterator[bytes]"
    signature = method_signature(operation)
    separator = ", " if "*" in signature else ", *, "
    signature = f"{signature}{separator}compressed: bool = False"
    lines = [f"{prefix}def {operation.name}({signature}) -> {iterator}:"]
    lines.extend(
        op_docstring(
            operation,
            extra_entries=[("compressed", COMPRESSED_ARG_DOC)],
        )
    )
    lines.append(
        f"{INDENT}spec = operations.build_{operation.name}("
        f"{render_passthrough(operation)})"
    )
    lines.append(f"{INDENT}self.log_request(spec)")
    if async_mode:
        # aclosing: an early aclose() of this generator must close the
        # transport stream too (async finalization is not deterministic)
        lines.append(
            f"{INDENT}async with aclosing("
            "self.stream(spec, auto_decompress=not compressed)"
            ") as source:"
        )
        lines.append(f"{INDENT * 2}async for chunk in source:")
        body_indent = INDENT * 3
    else:
        lines.append(
            f"{INDENT}source = self.stream(spec, auto_decompress=not compressed)"
        )
        lines.append(f"{INDENT}for chunk in source:")
        body_indent = INDENT * 2
    lines.append(f'{body_indent}self.log.debug("stream chunk: %d bytes", len(chunk))')
    lines.append(f"{body_indent}yield chunk")
    return lines


def emit_sse_method(operation: OperationDef, async_mode: bool) -> list[str]:
    prefix = "async " if async_mode else ""
    event_type = operation.response.type
    if event_type is None:
        raise ValueError(f"SSE operation {operation.name!r} has no event type")
    event_annotation = render_type_ref(event_type)
    iterator = (
        f"AsyncGenerator[{event_annotation}, None]"
        if async_mode
        else f"Iterator[{event_annotation}]"
    )
    loop = "async for" if async_mode else "for"
    lines = [
        f"{prefix}def {operation.name}("
        f"{method_signature(operation)}, deadline: float | None = None"
        f") -> {iterator}:",
    ]
    lines.extend(op_docstring(operation))
    lines.append(
        f"{INDENT}spec = operations.build_{operation.name}("
        f"{render_passthrough(operation)})"
    )
    lines.append(f"{INDENT}spec.deadline = deadline")
    # a deadline (monotonic seconds) bounds the whole subscription:
    # the socket read timeout covers silent gaps, while the per-chunk
    # check below covers streams kept alive by keepalive comments
    lines.append(f"{INDENT}if deadline is not None:")
    lines.append(
        f"{INDENT * 2}spec.read_timeout = max(0.0, deadline - time.monotonic())"
    )
    lines.append(f"{INDENT}self.log_request(spec)")
    lines.append(f"{INDENT}parser = SSEParser()")
    resume_argument = operation.response.resume_argument
    if resume_argument is None:
        raise ValueError(f"SSE operation {operation.name!r} has no resume argument")
    lines.append(f"{INDENT}last_seen = {resume_argument}")
    if async_mode:
        # aclosing: an early aclose() of this generator must close the
        # transport stream too (async finalization is not deterministic)
        lines.append(f"{INDENT}async with aclosing(self.stream(spec)) as source:")
        lines.append(f"{INDENT * 2}{loop} chunk in source:")
        base = INDENT * 3
    else:
        lines.append(f"{INDENT}{loop} chunk in self.stream(spec):")
        base = INDENT * 2
    lines.append(f"{base}if deadline is not None and time.monotonic() >= deadline:")
    lines.append(
        f'{base + INDENT}raise TimeoutError(f"{operation.name} exceeded its deadline")'
    )
    lines.append(f"{base}for frame in parser.feed(chunk):")
    lines.append(f"{base + INDENT}if frame.id is not None:")
    lines.append(f"{base + INDENT * 2}last_seen = frame.id")
    lines.append(f"{base + INDENT}event = decode_event_frame(frame, last_seen)")
    lines.append(f"{base + INDENT}if event is None:")
    lines.append(f"{base + INDENT * 2}continue")
    lines.append(f'{base + INDENT}self.log.debug("sse event: %r", event)')
    lines.append(f"{base + INDENT}yield event")
    return lines


PAGE_SIZE_DOC = (
    "how many records to fetch per request (the server caps a page at {maximum})"
)
ITER_LIMIT_DOC = "stop after this many records in total; None iterates everything"


def emit_iter_method(operation: OperationDef, async_mode: bool) -> list[str]:
    """A lazy pagination iterator over one of the list operations.

    Mirrors the list method's filters (minus limit/offset), fetching
    pages transparently as the caller consumes items.
    """
    pagination = operation.pagination
    if pagination is None:
        raise ValueError(f"operation {operation.name!r} is not paginated")
    accessor = "".join(f".{part}" for part in pagination.items_path)
    item = render_type_ref(pagination.item_type)
    maximum = pagination.max_page_size
    filters = [
        argument
        for argument in operation.arguments
        if argument.name not in (pagination.limit_argument, pagination.offset_argument)
    ]
    prefix = "async " if async_mode else ""
    awaited = "await " if async_mode else ""
    iterator = f"AsyncGenerator[{item}, None]" if async_mode else f"Iterator[{item}]"
    signature_parts = ["self", "*"]
    for argument in filters:
        default = render_argument_default(argument)
        if default is None:
            raise ValueError(
                f"pagination filter {operation.name}.{argument.name} is required"
            )
        signature_parts.append(
            f"{argument.name}: {render_argument_annotation(argument)} = {default}"
        )
    signature_parts.append(f"page_size: int = {maximum}")
    signature_parts.append("limit: int | None = None")
    passthrough = ", ".join(
        [
            *(f"{argument.name}={argument.name}" for argument in filters),
            f"{pagination.limit_argument}=size",
            f"{pagination.offset_argument}=offset",
        ]
    )
    lines = [
        f"{prefix}def {pagination.iterator_name}({', '.join(signature_parts)})"
        f" -> {iterator}:",
    ]
    entries = [
        (argument.name, text)
        for argument in filters
        if (text := render_documentation(argument.documentation))
    ]
    entries.append(("page_size", PAGE_SIZE_DOC.format(maximum=maximum)))
    entries.append(("limit", ITER_LIMIT_DOC))
    lines.extend(
        build_docstring(
            INDENT,
            f"Iterate over {operation.name}() results across pages.",
            "Offset pagination happens transparently as items are"
            " consumed; breaking out of the loop stops fetching. Note"
            " that offset pagination is not a snapshot - records"
            " created or deleted between page fetches may shift, so"
            " items can repeat or be skipped under concurrent"
            " modification.",
            "Args",
            entries,
        )
    )
    lines.extend(
        [
            # the server silently caps pages at its maximum: a larger
            # page_size would make the short-page check end the
            # iteration early and lose the tail
            f"{INDENT}if not 1 <= page_size <= {maximum}:",
            f"{INDENT * 2}raise ValueError(",
            f'{INDENT * 3}"page_size must be between 1 and {maximum}"',
            f"{INDENT * 2})",
            f"{INDENT}fetched = 0",
            f"{INDENT}offset = 0",
            f"{INDENT}while True:",
            f"{INDENT * 2}size = (",
            (
                f"{INDENT * 3}page_size"
                f" if limit is None else min(page_size, limit - fetched)"
            ),
            f"{INDENT * 2})",
            f"{INDENT * 2}if size <= 0:",
            f"{INDENT * 3}return",
            f"{INDENT * 2}page = ("
            f"{awaited}self.{operation.name}({passthrough})){accessor}",
            f"{INDENT * 2}if isinstance(page, EllipsisType) or not page:",
            f"{INDENT * 3}return",
            f"{INDENT * 2}for item in page:",
            f"{INDENT * 3}yield item",
            f"{INDENT * 3}fetched += 1",
            f"{INDENT * 3}if limit is not None and fetched >= limit:",
            f"{INDENT * 4}return",
            f"{INDENT * 2}if len(page) < size:",
            f"{INDENT * 3}return",
            f"{INDENT * 2}offset += len(page)",
        ]
    )
    return lines


def emit_api_methods(ir: SpecIR, async_mode: bool) -> list[str]:
    blocks: list[str] = []
    for operation in ir.operations:
        if operation.response.mode is ResponseMode.SSE:
            lines = emit_sse_method(operation, async_mode)
        elif operation.response.mode is ResponseMode.BYTE_STREAM:
            lines = emit_stream_only_method(operation, async_mode)
        else:
            lines = emit_call_method(operation, async_mode)
        blocks.append("\n".join(f"{INDENT}{line}" if line else "" for line in lines))
        if operation.response.mode is ResponseMode.BYTES:
            lines = emit_stream_method(operation, async_mode)
            blocks.append(
                "\n".join(f"{INDENT}{line}" if line else "" for line in lines)
            )
        if operation.pagination is not None:
            lines = emit_iter_method(operation, async_mode)
            blocks.append(
                "\n".join(f"{INDENT}{line}" if line else "" for line in lines)
            )
    return blocks


def render_base(ir: SpecIR) -> str:
    sync_methods = emit_api_methods(ir, async_mode=False)
    async_methods = emit_api_methods(ir, async_mode=True)
    sync_source = "\n\n".join([SYNC_CLASS_HEADER.rstrip("\n"), *sync_methods])
    async_source = "\n\n".join([ASYNC_CLASS_HEADER.rstrip("\n"), *async_methods])
    model_names = used_names(
        sync_source,
        [*ir.model_names, "OperationEvent", "OperationStatus"],
    )
    header = BASE_HEADER.format(
        note=GENERATED_NOTE,
        model_imports=import_block(".models", model_names),
    )
    return "\n\n\n".join([header.rstrip("\n"), sync_source, async_source]) + "\n"


# ---------------------------------------------------------------------------
# __init__.py
# ---------------------------------------------------------------------------

INIT_HEADER = '''"""Contree API client, generated from the OpenAPI specification.

Pick a backend module and import its ``ContreeClient``::

    from contree_client.requests import ContreeClient

Synchronous backends export ``ContreeClient``, asynchronous ones
export ``ContreeAsyncClient``: ``contree_client.http`` (stdlib
http.client, no extra dependencies), ``contree_client.urllib3`` and
``contree_client.requests`` (sync), ``contree_client.httpx`` (both)
and ``contree_client.aiohttp`` (async).  All of them share the
interface of :class:`contree_client.types.ContreeSyncClient` or
:class:`contree_client.types.ContreeAsyncClient` - annotate against
those base classes to stay backend-agnostic.

When any installed backend will do, let the package pick one::

    from contree_client.sync import ContreeClient
    from contree_client.asyncio import ContreeAsyncClient

{note}
"""
'''

EXCEPTION_NAMES = [
    "APIConnectionError",
    "APIStatusError",
    "AuthenticationError",
    "BadRequestError",
    "ConflictError",
    "ContreeError",
    "GoneError",
    "ServerError",
    "NotFoundError",
    "PermissionDeniedError",
    "RateLimitError",
    "TooEarlyError",
    "UnprocessableEntityError",
]


def render_init(ir: SpecIR) -> str:
    model_names = sorted(
        [
            *ir.model_names,
            "ACTIVE_STATUSES",
            "ContreeModel",
            "EventData",
            "OperationEventType",
            "OperationStatus",
            "TERMINAL_STATUSES",
            "decode_chunk",
            "decode_stream",
            "parse_datetime",
        ]
    )
    exports = sorted(
        model_names
        + EXCEPTION_NAMES
        + [
            "DEFAULT_BASE_URL",
            "Profile",
            "ProfileError",
            "RequestSpec",
            "ResponseData",
            "RetryPolicy",
            "load_profiles",
            "resolve_profile",
        ]
    )
    all_block = "\n".join(f'{INDENT}"{name}",' for name in exports)
    imports = "\n".join(
        [
            import_block(".exceptions", EXCEPTION_NAMES),
            import_block(".models", model_names),
            import_block(
                ".profiles",
                ["Profile", "ProfileError", "load_profiles", "resolve_profile"],
            ),
            import_block(
                ".runtime",
                ["RequestSpec", "ResponseData", "RetryPolicy"],
            ),
            import_block(".spec_info", ["DEFAULT_BASE_URL"]),
        ]
    )
    parts = [
        INIT_HEADER.format(note=GENERATED_NOTE).rstrip("\n"),
        imports,
        f"__all__ = [\n{all_block}\n]",
    ]
    return "\n\n".join(parts) + "\n"


# ---------------------------------------------------------------------------
# spec_info.py and entry point
# ---------------------------------------------------------------------------


def render_spec_info(ir: SpecIR) -> str:
    # the full spec is embedded as the module docstring inside an rst
    # literal block, so autodoc renders it as highlighted YAML instead
    # of parsing it as reStructuredText; backslashes and triple quotes
    # are escaped to keep the docstring a valid literal
    spec_doc = ir.spec_text.replace("\\", "\\\\").replace('"""', '\\"""')
    indented = "\n".join(
        f"{INDENT}{line}" if line.strip() else "" for line in spec_doc.splitlines()
    )
    return (
        f'"""The OpenAPI specification this package was generated from.\n'
        f"\n{GENERATED_NOTE}\n\n"
        ".. code-block:: yaml\n\n"
        f'{indented}\n"""\n\n'
        # the docstring embeds the raw spec verbatim: its long lines,
        # trailing whitespace and unicode are the upstream document,
        # not our code - suppressed file-wide by necessity
        "# ruff: noqa: E501, W291, W293, RUF002\n\n"
        "from __future__ import annotations\n\n"
        f"DEFAULT_BASE_URL = {ir.default_base_url!r}\n\n"
        "# sha256 of the exact OpenAPI document this package was built"
        " from -\n# the build input provenance\n"
        f"SPEC_SHA256 = {ir.spec_sha256!r}\n"
    )


def run_ruff(paths: list[Path]) -> None:
    """Fix, format and *gate* the generated files.

    Ruff is a build dependency; a missing binary or an unfixable
    finding must fail the generation instead of shipping an
    unvalidated artifact.
    """
    if importlib.util.find_spec("ruff") is None:
        raise RuntimeError(
            "ruff is not installed; it is required to validate generated code"
        )
    files = [str(path) for path in paths]
    ruff = [sys.executable, "-m", "ruff"]
    # format first: long generated lines are wrapped before linting,
    # then the lint pass fixes what formatting does not cover (imports)
    subprocess.run([*ruff, "format", "--quiet", *files], check=True)
    subprocess.run([*ruff, "check", "--fix", "--quiet", *files], check=False)
    subprocess.run([*ruff, "format", "--quiet", *files], check=True)
    # the mandatory gate: anything unfixable stops the build
    subprocess.run([*ruff, "check", "--quiet", *files], check=True)


class PythonEmitter(Emitter):
    """Renders the contree_client package; gated by ruff + py_compile."""

    files = GENERATED_FILES

    def render(self, ir: SpecIR) -> dict[str, str]:
        return {
            "__init__.py": render_init(ir),
            "base.py": render_base(ir),
            "models.py": render_models(ir),
            "operations.py": render_operations(ir),
            "spec_info.py": render_spec_info(ir),
        }

    def validate(self, paths: list[Path]) -> None:
        run_ruff(paths)
        for path in paths:
            py_compile.compile(str(path), doraise=True)


def generate(spec_source: str | Path, package_dir: Path) -> Path:
    """Generate the Python contree_client package."""
    return PythonEmitter().generate(spec_source, package_dir)
