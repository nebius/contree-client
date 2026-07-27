"""Intermediate representation: schemas -> dataclasses, paths -> operations."""

from __future__ import annotations

import json
import re
import textwrap
from dataclasses import dataclass, field
from typing import Any

from .loader import Spec, ref_name
from .naming import pascal_case, snake_case

INDENT = "    "

# Schemas that never become dataclasses.
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
TIME_PARAM_ANNOTATION = "str | int | float | datetime | None"


# ---------------------------------------------------------------------------
# Type references
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TypeRef:
    annotation: str
    parse_template: str = "{v}"
    dump_template: str = "{v}"

    @property
    def identity(self) -> bool:
        return self.parse_template == "{v}" and self.dump_template == "{v}"

    def parse(self, expr: str) -> str:
        return self.parse_template.replace("{v}", expr)

    def dump(self, expr: str) -> str:
        return self.dump_template.replace("{v}", expr)


STR = TypeRef("str")
INT = TypeRef("int")
FLOAT = TypeRef("float")
BOOL = TypeRef("bool")
ANY = TypeRef("Any")
DATETIME = TypeRef(
    "datetime",
    parse_template="parse_datetime({v})",
    dump_template="{v}.isoformat()",
)


def model_ref(name: str) -> TypeRef:
    return TypeRef(
        name,
        parse_template=name + ".from_dict({v})",
        dump_template="{v}.to_dict()",
    )


def list_of(item: TypeRef) -> TypeRef:
    annotation = f"list[{item.annotation}]"
    if item.identity:
        return TypeRef(annotation)
    return TypeRef(
        annotation,
        parse_template="[" + item.parse("item") + " for item in {v}]",
        dump_template="[" + item.dump("item") + " for item in {v}]",
    )


def dict_of(value: TypeRef) -> TypeRef:
    annotation = f"dict[str, {value.annotation}]"
    if value.identity:
        return TypeRef(annotation)
    return TypeRef(
        annotation,
        parse_template=(
            "{key: " + value.parse("value") + " for key, value in {v}.items()}"
        ),
        dump_template=(
            "{key: " + value.dump("value") + " for key, value in {v}.items()}"
        ),
    )


def literal_of(values: list[str]) -> TypeRef:
    inner = ", ".join(repr(v) for v in values)
    return TypeRef(f"Literal[{inner}]")


OPERATION_STATUS = TypeRef(
    "OperationStatus",
    parse_template="OperationStatus({v})",
    dump_template="{v}.value",
)


# Explicit strategies for unions the generic rules cannot express:
# discriminated unions (no machine-readable discriminator in the spec)
# and deliberately untyped payloads.
FIELD_TYPE_OVERRIDES: dict[tuple[str, str], TypeRef] = {
    ("Error", "error"): ANY,
    # the wire format is an octal string; the constructor also accepts
    # a plain int (0o644) which __post_init__ normalizes
    ("FileSpec", "mode"): TypeRef("str | int"),
    ("OperationResponse", "metadata"): TypeRef(
        "OperationInstanceMetadata | ImageImportMetadata",
        parse_template=(
            "OperationInstanceMetadata.from_dict({v})"
            ' if data.get("kind") == "instance"'
            " else ImageImportMetadata.from_dict({v})"
        ),
        dump_template="{v}.to_dict()",
    ),
    ("OperationEvent", "data"): TypeRef(
        "EventData | dict[str, Any]",
        parse_template='parse_event_data(data["type"], {v})',
    ),
}


# ---------------------------------------------------------------------------
# Dataclass model definitions
# ---------------------------------------------------------------------------


LINE_LIMIT = 88
# implicit-concatenation chunks are laid out by the formatter at the
# metadata-value depth: dict items sit at column 12, chunks at 16
METADATA_ITEM_COLUMN = 12
CHUNK_WIDTH = LINE_LIMIT - METADATA_ITEM_COLUMN - 4


def string_literal(value: object, offset: int = 0) -> str:
    """Repr a value; strings that overflow the line limit become
    parenthesized implicit concatenation.

    The formatter cannot split a single long string token, so
    spec-provided prose is chunked at word boundaries. *offset* is the
    rendered column the literal starts at (plus the trailing comma):
    a string is kept inline whenever it genuinely fits there.
    """
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


@dataclass
class FieldDef:
    """One model field.

    Optional fields are tri-state: `...` (unset, omitted on the wire so
    the server default applies), `None` (an explicit JSON null) or a
    value. Required nullable fields are just `T | None`.
    """

    py_name: str
    json_name: str
    type: TypeRef
    required: bool
    nullable: bool
    description: str = ""
    example: str = ""
    example_value: Any = None
    has_example: bool = False
    default_value: Any = None
    has_default: bool = False

    @property
    def doc(self) -> str:
        parts = [self.description] if self.description else []
        if self.example:
            parts.append(f"Example: ``{self.example}``.")
        return " ".join(parts)

    @property
    def metadata_literal(self) -> str:
        items: list[str] = []
        for key, value, present in (
            ("description", self.description, bool(self.description)),
            ("example", self.example_value, self.has_example),
            ("default", self.default_value, self.has_default),
        ):
            if not present:
                continue
            offset = METADATA_ITEM_COLUMN + len(f'"{key}": ') + len(",")
            items.append(f'"{key}": {string_literal(value, offset)}')
        return "{" + ", ".join(items) + "}" if items else ""

    @property
    def annotation(self) -> str:
        base = self.type.annotation
        if self.nullable and not base.endswith("| None"):
            base = f"{base} | None"
        if not self.required:
            base = f"{base} | EllipsisType"
        return base

    @property
    def declaration(self) -> str:
        metadata = self.metadata_literal
        if not metadata:
            decl = f"{self.py_name}: {self.annotation}"
            if not self.required:
                decl += " = ..."
            return decl
        arguments = [] if self.required else ["default=..."]
        arguments.append(f"metadata={metadata}")
        return f"{self.py_name}: {self.annotation} = field({', '.join(arguments)})"

    @property
    def parse_expr(self) -> str:
        src = f'data["{self.json_name}"]'
        present = f'data.get("{self.json_name}")'
        missing = f'data.get("{self.json_name}", ...)'
        if self.required:
            if self.type.identity:
                return src if not self.nullable else present
            if not self.nullable:
                return self.type.parse(src)
            return f"({self.type.parse(src)}) if {present} is not None else None"
        if self.type.identity:
            return missing
        return f"({self.type.parse(src)}) if {present} is not None else {missing}"


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

# Hand-written methods appended to specific generated model classes.
# Values are .format() templates: {name} is the class name.
EXTRA_CLASS_METHODS: dict[str, str] = {
    "StreamRepr": STREAM_VALUE_METHODS,
    "EventDataStream": STREAM_VALUE_METHODS,
    "FileSpec": FILESPEC_MODE_METHODS,
}


@dataclass
class ClassDef:
    name: str
    description: str
    fields: list[FieldDef]

    @property
    def ordered_fields(self) -> list[FieldDef]:
        required = [f for f in self.fields if f.required]
        optional = [f for f in self.fields if not f.required]
        return required + optional

    @property
    def needs_parse_override(self) -> bool:
        """The default field copying is enough when every field is
        wire-identical; nested models, datetimes and discriminated
        unions need generated parsing."""
        return any(not fld.type.identity for fld in self.fields)

    def render(self) -> str:
        lines: list[str] = ["@dataclass", f"class {self.name}(ContreeModel):"]
        if self.description:
            lines.extend(
                build_docstring(
                    INDENT,
                    self.description,
                    description_body(self.description),
                )
            )
            lines.append("")
        lines.extend(f"{INDENT}{fld.declaration}" for fld in self.ordered_fields)
        if self.needs_parse_override:
            lines.append("")
            lines.append(f"{INDENT}@classmethod")
            lines.append(
                f"{INDENT}def parse_fields(cls, data: dict[str, Any])"
                " -> dict[str, Any]:"
            )
            lines.append(f"{INDENT * 2}return {{")
            lines.extend(
                f'{INDENT * 3}"{fld.py_name}": {fld.parse_expr},'
                for fld in self.ordered_fields
            )
            lines.append(f"{INDENT * 2}}}")
        extra = EXTRA_CLASS_METHODS.get(self.name)
        if extra:
            lines.append(extra.format(name=self.name).rstrip("\n"))
        return "\n".join(lines)


def render_docstring(text: str, indent: str) -> list[str]:
    summary = summary_line(text)
    wrapped = textwrap.wrap(summary, width=72)
    if not wrapped:
        return []
    if len(wrapped) == 1:
        return [f'{indent}"""{wrapped[0]}"""']
    lines = [f'{indent}"""{wrapped[0]}']
    lines.extend(f"{indent}{line}" for line in wrapped[1:])
    lines.append(f'{indent}"""')
    return lines


def summary_line(text: str) -> str:
    for line in text.strip().splitlines():
        line = line.strip()
        if line:
            return line.rstrip(".") + "."
    return ""


def description_body(text: str) -> str:
    """Everything after the line used as the docstring summary."""
    lines = text.strip().splitlines()
    if len(lines) <= 1:
        return ""
    return "\n".join(lines[1:]).strip("\n")


INLINE_CODE_RE = re.compile(r"(?<!`)`([^`\n]+)`(?!`)")
LITERAL_SPAN_RE = re.compile(r"``[^`\n]+``")
NBSP = "\x00"


def sanitize_doc(text: str, escape: bool = True) -> str:
    """Prepare a spec description for embedding into a docstring.

    Escapes backslashes and triple quotes (unless the text is used
    outside of source code), and converts markdown inline code
    (single backticks) into reST literals (double backticks) - reST
    would otherwise parse it as title references and leak raw
    `<...>` markers into the builders.
    """
    if escape:
        text = text.replace("\\", "\\\\").replace('"""', '\\"""')
    # the spec uses U+2011 NON-BREAKING HYPHEN in prose; normalize it
    # so linters do not flag ambiguous unicode in docstrings
    text = text.replace("\u2011", "-")
    return INLINE_CODE_RE.sub(r"``\1``", text)


def protect_literals(text: str) -> str:
    """Make ``...`` spans unbreakable for textwrap."""
    return LITERAL_SPAN_RE.sub(lambda m: m.group(0).replace(" ", NBSP), text)


def restore_literals(lines: list[str]) -> list[str]:
    return [line.replace(NBSP, " ") for line in lines]


def format_example(value: Any) -> str:
    """Render a spec example compactly for `Example: ...` doc notes."""
    if value is None:
        return ""
    if isinstance(value, str):
        rendered = value
    else:
        # compact separators: fewer spaces means fewer wrap points
        # inside the ``...`` literal
        rendered = json.dumps(
            value,
            ensure_ascii=False,
            default=str,
            separators=(",", ":"),
        )
    rendered = " ".join(rendered.split())
    if not rendered:
        return ""
    if len(rendered) > 60:
        rendered = rendered[:57] + "..."
    return rendered


BULLET_RE = re.compile(r"^(\s*)([-*+]\s+|\d+[.)]\s+)?")


def wrap_doc_line(raw: str, width: int = 72) -> list[str]:
    raw = raw.rstrip()
    if len(raw) <= width:
        return [raw]
    match = BULLET_RE.match(raw)
    assert match is not None
    pad = len(match.group(1))
    # continuation of a list item aligns under its text, otherwise
    # reST turns the indented follow-up lines into definition lists
    hang = " " * (pad + len(match.group(2) or ""))
    first, *rest = textwrap.wrap(
        protect_literals(raw.strip()),
        width=max(20, width - pad),
        break_long_words=False,
        break_on_hyphens=False,
    )
    lines = [" " * pad + first]
    lines.extend(hang + piece for piece in rest)
    return restore_literals(lines)


def doc_block_lines(text: str, escape: bool = True) -> list[str]:
    """Wrap a description preserving its original line structure.

    Markdown tables and fenced code blocks are emitted verbatim as
    reST literal blocks - every Sphinx builder can render those, while
    `|`-rows and fences would otherwise misparse as line blocks.
    """
    lines: list[str] = []
    raw_lines = sanitize_doc(text, escape=escape).strip().splitlines()

    def literal_block(body: list[str]) -> None:
        if lines and lines[-1]:
            lines.append("")
        lines.append("::")
        lines.append("")
        lines.extend(f"    {item}".rstrip() for item in body)
        lines.append("")

    index = 0
    last_pad = 0
    while index < len(raw_lines):
        raw = raw_lines[index].rstrip()
        pad = len(raw) - len(raw.lstrip())
        # reST demands a blank line when a list ends by dedenting
        if raw and lines and lines[-1] and pad < last_pad:
            lines.append("")
        if raw:
            last_pad = pad
        if raw.lstrip().startswith("|"):
            table: list[str] = []
            while index < len(raw_lines) and raw_lines[index].lstrip().startswith("|"):
                table.append(raw_lines[index].strip())
                index += 1
            literal_block(table)
            continue
        if raw.lstrip().startswith("```"):
            fence: list[str] = []
            index += 1
            while index < len(raw_lines) and not raw_lines[index].lstrip().startswith(
                "```"
            ):
                fence.append(raw_lines[index].rstrip())
                index += 1
            index += 1  # closing fence
            literal_block(fence)
            continue
        lines.extend(wrap_doc_line(raw))
        index += 1
    return lines


def doc_entry_lines(name: str, text: str) -> list[str]:
    """One `name: text` entry of an Args/Attributes napoleon section."""
    body = " ".join(sanitize_doc(text).split())
    return restore_literals(
        textwrap.wrap(
            protect_literals(f"{name}: {body}"),
            width=68,
            initial_indent="    ",
            subsequent_indent="        ",
            break_long_words=False,
            break_on_hyphens=False,
        )
    )


def build_docstring(
    indent: str,
    summary: str,
    description: str = "",
    section_title: str = "",
    entries: list[tuple[str, str]] | None = None,
) -> list[str]:
    """Render a docstring: summary, spec description, napoleon section."""
    relative = textwrap.wrap(sanitize_doc(summary_line(summary)), width=72)
    if not relative:
        relative = [""]
    if description:
        relative.append("")
        relative.extend(doc_block_lines(description))
    if entries:
        relative.append("")
        relative.append(f"{section_title}:")
        for name, text in entries:
            relative.extend(doc_entry_lines(name, text))
    if len(relative) == 1:
        return [f'{indent}"""{relative[0]}"""']
    lines = [f'{indent}"""{relative[0]}']
    lines.extend(f"{indent}{line}" if line else "" for line in relative[1:])
    lines.append(f'{indent}"""')
    return lines


# ---------------------------------------------------------------------------
# Schema conversion
# ---------------------------------------------------------------------------


class SchemaConverter:
    def __init__(self, spec: Spec) -> None:
        self.spec = spec
        self.classes: list[ClassDef] = []
        self.known: set[str] = set()
        self.status_values = operation_status_values(spec)

    def convert_all(self) -> None:
        for name, schema in self.spec.schemas.items():
            if name in SKIP_SCHEMAS:
                continue
            self.convert_object(name, schema)

    def class_by_name(self, name: str) -> ClassDef:
        for cls in self.classes:
            if cls.name == name:
                return cls
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
            py_name = snake_case(json_name)
            if py_name != json_name:
                # to_dict serializes via dataclasses.asdict, which uses
                # the python field names as wire keys
                raise ValueError(
                    f"{name}.{json_name}: python field name {py_name!r}"
                    " differs from the wire name; asdict-based"
                    " serialization requires them to match"
                )
            fields.append(
                FieldDef(
                    py_name=py_name,
                    json_name=json_name,
                    type=type_ref,
                    required=json_name in required,
                    nullable=nullable,
                    description=str(prop.get("description", "")).strip(),
                    example=format_example(prop.get("example")),
                    example_value=prop.get("example"),
                    has_example="example" in prop,
                    default_value=prop.get("default"),
                    has_default="default" in prop,
                )
            )
        self.classes.append(
            ClassDef(
                name=name,
                description=schema.get("description", ""),
                fields=fields,
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
            return override, nullable

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
                "string": "str",
                "integer": "int",
                "number": "float",
                "boolean": "bool",
            }
            names: list[str] = []
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
                if mapped not in names:
                    names.append(mapped)
            return TypeRef(" | ".join(names)), nullable

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
            return TypeRef("dict[str, Any]"), nullable
        return ANY, nullable

    def type_for_named(self, rname: str) -> TypeRef:
        if rname in STRING_ALIAS_SCHEMAS:
            return STR
        if rname == "OperationEventType":
            return TypeRef("OperationEventType")
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


@dataclass
class ArgDef:
    py_name: str
    annotation: str
    default: str | None  # None means required
    doc: str = ""


@dataclass
class ParamDef:
    """A language-neutral request parameter descriptor.

    ``build_src`` bakes the Python builder; non-Python emitters render
    their builders from these instead. *style* names the encoding:
    ``str`` | ``int`` | ``flag`` (presence -> "1") | ``time``
    (datetime/interval) | ``enum`` | ``status``.
    """

    json_name: str
    py_name: str
    where: str  # path | query | header
    style: str = "str"
    required: bool = False


@dataclass
class BodyFieldDef:
    """One property of an inline (schema-less) JSON request body."""

    json_name: str
    py_name: str
    annotation: str
    required: bool


@dataclass
class OpDef:
    name: str
    http_method: str
    path: str
    summary: str
    description: str = ""
    args: list[ArgDef] = field(default_factory=list)
    kind: str = "call"  # call | bytes | sse
    stream_variant: bool = False
    return_annotation: str = "None"
    build_src: str = ""
    parse_src: str = ""
    # the server-side cap of the `limit` parameter (spec `maximum`);
    # pagination iterators validate page_size against it
    page_limit_max: int | None = None
    # language-neutral request/response structure (non-Python emitters
    # render from these; the Python emitter uses build_src/parse_src)
    params: list[ParamDef] = field(default_factory=list)
    body_kind: str | None = None  # json_model | json_inline | binary
    body_model: str | None = None
    body_fields: list[BodyFieldDef] = field(default_factory=list)
    response_kind: str = "none"
    response_model: str | None = None
    success_status: str = "200"

    @property
    def signature(self) -> str:
        required = [a for a in self.args if a.default is None]
        optional = [a for a in self.args if a.default is not None]
        parts = [f"{a.py_name}: {a.annotation}" for a in required]
        if optional:
            parts.append("*")
            parts.extend(f"{a.py_name}: {a.annotation} = {a.default}" for a in optional)
        return ", ".join(parts)

    @property
    def passthrough(self) -> str:
        return ", ".join(f"{a.py_name}={a.py_name}" for a in self.args)


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


class OperationBuilder:
    def __init__(self, spec: Spec, converter: SchemaConverter) -> None:
        self.spec = spec
        self.converter = converter

    def build_all(self) -> list[OpDef]:
        ops: list[OpDef] = []
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
                ops.append(self.build_op(op_id, method, path, raw, shared_params))
        return ops

    def build_op(
        self,
        op_id: str,
        method: str,
        path: str,
        raw: dict[str, Any],
        shared_params: list[dict[str, Any]],
    ) -> OpDef:
        name = OPERATION_NAME_OVERRIDES.get(op_id) or snake_case(op_id)
        op = OpDef(
            name=name,
            http_method=method.upper(),
            path=path,
            summary=raw.get("summary", ""),
            description=str(raw.get("description", "")).strip(),
        )

        path_args: list[ArgDef] = []
        query_lines: list[str] = []
        header_lines: list[str] = []
        path_py_names: dict[str, str] = {}

        params = [
            self.spec.deref(p) for p in list(shared_params) + raw.get("parameters", [])
        ]
        for param in params:
            if param["name"] in SKIP_PARAMS:
                continue
            if param["name"] == "limit":
                maximum = (param.get("schema") or {}).get("maximum")
                if isinstance(maximum, int):
                    op.page_limit_max = maximum
            self.add_param(
                op, param, path_args, query_lines, header_lines, path_py_names
            )

        body_lines, body_present = self.add_body(op, raw)

        kind, success, model = self.response_kind(op_id, method, raw["responses"])
        if op_id in STREAM_ONLY_OPERATIONS:
            kind = "stream"
        accept = "text/event-stream" if kind == "sse" else None
        op.kind = {"sse": "sse", "bytes": "bytes", "stream": "stream"}.get(kind, "call")
        op.stream_variant = kind == "bytes"
        op.return_annotation = self.return_annotation(kind, model)
        op.response_kind = kind
        op.response_model = model
        op.success_status = success

        # order args: path/required first is handled by OpDef.signature
        op.args = path_args + op.args

        build_body: list[str] = []
        if query_lines:
            build_body.append("query: dict[str, str] = {}")
            build_body.extend(query_lines)
        if header_lines:
            build_body.append("headers: dict[str, str] = {}")
            build_body.extend(header_lines)
        build_body.extend(body_lines)
        build_body.append(self.render_path_line(path, path_py_names))

        idempotent = op.http_method in ("GET", "HEAD", "PUT", "DELETE")
        spec_args = [
            f'method="{op.http_method}"',
            "path=request_path",
            f"idempotent={idempotent}",
        ]
        if query_lines:
            spec_args.append("query=query")
        if header_lines:
            spec_args.append("headers=headers")
        if body_present == "json":
            spec_args.append("body=json.dumps(payload).encode()")
            spec_args.append('content_type="application/json"')
        elif body_present == "binary":
            spec_args.append("body=content")
            spec_args.append('content_type="application/octet-stream"')
        if accept:
            spec_args.append(f'accept="{accept}"')
        build_body.append(f"return RequestSpec({', '.join(spec_args)})")

        op.build_src = render_function(
            f"build_{op.name}",
            op.signature,
            "RequestSpec",
            f"Build the request for `{op.http_method} {path}`.",
            build_body,
        )
        parse_body = self.parse_body(kind, success, model)
        if parse_body:
            op.parse_src = render_function(
                f"parse_{op.name}",
                "response: ResponseData",
                op.return_annotation,
                f"Parse the response of `{op.http_method} {path}`.",
                parse_body,
            )
        return op

    def add_param(
        self,
        op: OpDef,
        param: dict[str, Any],
        path_args: list[ArgDef],
        query_lines: list[str],
        header_lines: list[str],
        path_py_names: dict[str, str],
    ) -> None:
        json_name = param["name"]
        where = param["in"]
        schema = self.spec.deref(param.get("schema", {}))
        py = snake_case(json_name)
        doc = str(param.get("description", "")).strip()
        example = format_example(param.get("example", schema.get("example")))
        if example:
            doc = f"{doc} Example: ``{example}``.".strip()

        if where == "path":
            annotation = "int" if schema.get("type") == "integer" else "str"
            path_args.append(ArgDef(py, annotation, None, doc=doc))
            path_py_names[json_name] = py
            op.params.append(ParamDef(json_name, py, "path", annotation, required=True))
            return

        if where == "header":
            op.args.append(ArgDef(py, "int | None", "None", doc=doc))
            header_lines.append(f"if {py} is not None:")
            header_lines.append(f'{INDENT}headers["{json_name}"] = str({py})')
            op.params.append(ParamDef(json_name, py, "header", "int"))
            return

        # query parameters
        is_flag = schema.get("enum") == ["", "0", "1"] or json_name == "tagged"
        if is_flag:
            op.args.append(ArgDef(py, "bool", "False", doc=doc))
            query_lines.append(f"if {py}:")
            query_lines.append(f'{INDENT}query["{json_name}"] = "1"')
            op.params.append(ParamDef(json_name, py, "query", "flag"))
            return

        if json_name in ("since", "until") and schema.get("type") == "string":
            op.args.append(ArgDef(py, TIME_PARAM_ANNOTATION, "None", doc=doc))
            query_lines.append(f"if {py} is not None:")
            query_lines.append(
                f'{INDENT}query["{json_name}"] = format_time_param({py})'
            )
            op.params.append(ParamDef(json_name, py, "query", "time"))
            return

        required = bool(param.get("required"))
        if schema.get("type") == "integer":
            annotation, value, style = "int", f"str({py})", "int"
        elif schema.get("enum"):
            values = schema["enum"]
            if json_name == "status" and set(values) <= set(
                self.converter.status_values
            ):
                annotation, value, style = (
                    "OperationStatus | str",
                    f"str({py})",
                    "status",
                )
            else:
                annotation, value, style = literal_of(values).annotation, py, "enum"
        else:
            annotation, value, style = "str", py, "str"
        op.params.append(ParamDef(json_name, py, "query", style, required=required))
        if required:
            op.args.append(ArgDef(py, annotation, None, doc=doc))
            query_lines.append(f'query["{json_name}"] = {value}')
        else:
            op.args.append(ArgDef(py, f"{annotation} | None", "None", doc=doc))
            query_lines.append(f"if {py} is not None:")
            query_lines.append(f'{INDENT}query["{json_name}"] = {value}')

    def add_body(
        self,
        op: OpDef,
        raw: dict[str, Any],
    ) -> tuple[list[str], str | None]:
        request_body = raw.get("requestBody")
        if not request_body:
            return [], None
        content = request_body["content"]
        if "application/octet-stream" in content:
            op.args.append(
                ArgDef(
                    "content",
                    "bytes | IO[bytes]",
                    None,
                    doc="Raw file content: bytes or a binary stream.",
                )
            )
            op.body_kind = "binary"
            return [], "binary"
        schema = content["application/json"]["schema"]
        rname = ref_name(schema)
        if rname is not None:
            op.body_kind = "json_model"
            op.body_model = rname
            cls = self.converter.class_by_name(rname)
            ctor_args: list[str] = []
            for fld in cls.ordered_fields:
                default = None if fld.required else "..."
                op.args.append(
                    ArgDef(fld.py_name, fld.annotation, default, doc=fld.doc)
                )
                ctor_args.append(f"{fld.py_name}={fld.py_name}")
            line = f"payload = {rname}({', '.join(ctor_args)}).to_dict()"
            return [line], "json"
        # inline body object
        op.body_kind = "json_inline"
        lines = ["payload: dict[str, Any] = {}"]
        required = set(schema.get("required", []))
        for json_name, prop in schema.get("properties", {}).items():
            type_ref, _ = self.converter.field_type("", json_name, prop)
            py = snake_case(json_name)
            op.body_fields.append(
                BodyFieldDef(json_name, py, type_ref.annotation, json_name in required)
            )
            field_doc = str(prop.get("description", "")).strip()
            field_example = format_example(prop.get("example"))
            if field_example:
                field_doc = f"{field_doc} Example: ``{field_example}``.".strip()
            if json_name in required:
                op.args.append(ArgDef(py, type_ref.annotation, None, doc=field_doc))
                lines.append(f'payload["{json_name}"] = {type_ref.dump(py)}')
            else:
                op.args.append(
                    ArgDef(
                        py,
                        f"{type_ref.annotation} | EllipsisType",
                        "...",
                        doc=field_doc,
                    )
                )
                lines.append(f"if {py} is not ...:")
                lines.append(f'{INDENT}payload["{json_name}"] = {type_ref.dump(py)}')
        return lines, "json"

    def response_kind(
        self,
        op_id: str,
        method: str,
        responses: dict[str, Any],
    ) -> tuple[str, str, str | None]:
        """Return (kind, success_status, model_name)."""
        if method == "head":
            return "bool", "200", None
        if op_id == "inspectFindImageByTag":
            return "location", "302", None
        for code in ("200", "201"):
            response = responses.get(code)
            if response is None:
                continue
            content = response.get("content") or {}
            if "text/event-stream" in content:
                return "sse", code, None
            if "application/octet-stream" in content or "application/x-tar" in content:
                return "bytes", code, None
            if "application/json" in content:
                schema = content["application/json"]["schema"]
                if schema.get("type") == "array":
                    item = ref_name(schema["items"])
                    if item is None:
                        raise ValueError(f"unsupported array response in {op_id}")
                    return "list_model", code, item
                rname = ref_name(schema)
                if rname is not None:
                    return "model", code, rname
                scalar = self.single_scalar_field(schema)
                if scalar is None:
                    raise ValueError(f"unsupported inline response in {op_id}")
                field_name, py_type = scalar
                return f"{py_type}_field", code, field_name
            return "none", code, None
        for code in ("204", "202"):
            if code in responses:
                return "none", code, None
        raise ValueError(f"cannot infer response kind for {op_id}")

    def single_scalar_field(self, schema: dict[str, Any]) -> tuple[str, str] | None:
        """The (name, py_type) of a lone required scalar property.

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
        py_type = {"string": "str", "integer": "int"}.get(prop.get("type", ""))
        if py_type is None:
            return None
        return name, py_type

    def return_annotation(self, kind: str, model: str | None) -> str:
        if kind == "model":
            assert model is not None
            return model
        if kind == "list_model":
            return f"list[{model}]"
        return {
            "str_field": "str",
            "int_field": "int",
            "location": "str",
            "bool": "bool",
            "bytes": "bytes",
            "none": "None",
            "sse": "None",  # client-level annotation differs
            "stream": "None",  # client-level annotation differs
        }[kind]

    def parse_body(
        self,
        kind: str,
        success: str,
        model: str | None,
    ) -> list[str]:
        # any 2xx counts as success: the spec documents one code per
        # operation, but the server legitimately answers with siblings
        # (e.g. 200-with-body where 204 is documented)
        success_line = "if 200 <= response.status < 300:"
        raise_line = "raise error_for_response(response)"
        if kind in ("sse", "stream"):
            return []
        if kind == "model":
            return [
                success_line,
                f"{INDENT}return {model}.from_dict(json_object(response))",
                raise_line,
            ]
        if kind == "list_model":
            return [
                success_line,
                f"{INDENT}return [",
                f"{INDENT * 2}{model}.from_dict(item) for item in json_array(response)",
                f"{INDENT}]",
                raise_line,
            ]
        if kind in ("str_field", "int_field"):
            cast = kind.removesuffix("_field")
            return [
                success_line,
                f'{INDENT}return {cast}(json_object(response)["{model}"])',
                raise_line,
            ]
        if kind == "location":
            # the server may send the Location relative to the request
            # path (observed live); the image UUID is its last segment
            # either way, and that is the value callers actually want
            return [
                f"if response.status == {success}:",
                f'{INDENT}location = response.headers["location"]',
                f'{INDENT}return location.rstrip("/").rsplit("/", 1)[-1]',
                raise_line,
            ]
        if kind == "bool":
            return [
                success_line,
                f"{INDENT}return True",
                "if response.status == 404:",
                f"{INDENT}return False",
                raise_line,
            ]
        if kind == "bytes":
            return [
                success_line,
                f"{INDENT}return response.body",
                raise_line,
            ]
        if kind == "none":
            return [
                success_line,
                f"{INDENT}return None",
                raise_line,
            ]
        raise ValueError(kind)

    def render_path_line(self, path: str, path_py_names: dict[str, str]) -> str:
        if not path_py_names:
            return f'request_path = "{path}"'
        rendered = path
        for json_name, py in path_py_names.items():
            rendered = rendered.replace(
                "{" + json_name + "}",
                "{quote_path(" + py + ")}",
            )
        return f'request_path = f"{rendered}"'


# ---------------------------------------------------------------------------
# Whole-spec IR
# ---------------------------------------------------------------------------


@dataclass
class SpecIR:
    default_base_url: str
    spec_text: str
    spec_sha256: str
    classes: list[ClassDef]
    operations: list[OpDef]
    event_type_values: list[str]
    status_values: list[str]
    terminal_status_values: list[str]

    @property
    def class_names(self) -> list[str]:
        return [cls.name for cls in self.classes]


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
        classes=converter.classes,
        operations=operations,
        event_type_values=event_type_values,
        status_values=converter.status_values,
        terminal_status_values=terminal_status_values(spec),
    )
