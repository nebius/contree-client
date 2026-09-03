# ruff: noqa: E501
"""Rendering of the generated JavaScript contree-client package.

Emits ESM modules plus TypeScript declaration files into
``client-js/lib``, next to the hand-written runtime (runtime.js,
errors.js, profiles.js, testing.js). Method names are camelCase;
model fields and option-object keys stay snake_case - exactly the
names that travel on the wire.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import textwrap
from pathlib import Path

from api_generator.documentation import (
    doc_block_lines,
    documentation_text,
    protect_literals,
    restore_literals,
    sanitize_doc,
)
from api_generator.emitter import GENERATED_NOTE, Emitter
from api_generator.ir import (
    ArgumentDef,
    ArgumentPresence,
    BodyKind,
    DiscriminatorDef,
    FieldDef,
    ModelDef,
    ModelTrait,
    OperationDef,
    ParameterEncoding,
    ParameterLocation,
    ResponseMode,
    SpecIR,
    SuccessPolicy,
    TypeKind,
    TypeRef,
)

GENERATED_FILES = (
    "models.js",
    "models.d.ts",
    "operations.js",
    "operations.d.ts",
    "client.js",
    "client.d.ts",
    "specInfo.js",
    "specInfo.d.ts",
    "index.js",
    "index.d.ts",
)

HEADER = f"// {GENERATED_NOTE}\n"


def camel(name: str) -> str:
    head, *tail = name.split("_")
    return head + "".join(part.title() for part in tail)


def used_names(source: str, names: list[str]) -> list[str]:
    """The subset of *names* that literally occur in *source*."""
    return [
        name
        for name in sorted(set(names))
        if re.search(rf"\b{re.escape(name)}\b", source)
    ]


def ts_type(type_ref: TypeRef) -> str:
    """Render a semantic type as TypeScript."""
    atoms = {
        TypeKind.ANY: "unknown",
        TypeKind.STRING: "string",
        TypeKind.INTEGER: "number",
        TypeKind.NUMBER: "number",
        TypeKind.BOOLEAN: "boolean",
        TypeKind.DATETIME: "Date",
        TypeKind.BYTES: "Uint8Array",
        TypeKind.BINARY_STREAM: "Blob | ReadableStream<Uint8Array>",
    }
    atom = atoms.get(type_ref.kind)
    if atom is not None:
        return atom
    if type_ref.kind in (TypeKind.MODEL, TypeKind.ENUM, TypeKind.ALIAS):
        assert type_ref.name is not None
        return type_ref.name
    if type_ref.kind is TypeKind.LITERAL:
        return " | ".join(f'"{value}"' for value in type_ref.values)
    if type_ref.kind is TypeKind.LIST:
        return f"{ts_type(type_ref.arguments[0])}[]"
    if type_ref.kind is TypeKind.SEQUENCE:
        return f"readonly {ts_type(type_ref.arguments[0])}[]"
    if type_ref.kind is TypeKind.MAP:
        return f"Record<string, {ts_type(type_ref.arguments[0])}>"
    if type_ref.kind is TypeKind.UNION:
        rendered: list[str] = []
        for item in type_ref.arguments:
            value = ts_type(item)
            if value not in rendered:
                rendered.append(value)
        return " | ".join(rendered)
    raise ValueError(type_ref.kind)


def ordered_fields(model: ModelDef) -> list[FieldDef]:
    required = [field for field in model.fields if field.required]
    optional = [field for field in model.fields if not field.required]
    return required + optional


def parse_type(type_ref: TypeRef, expr: str) -> str | None:
    """Return a wire decoder expression, or None for identity."""
    if type_ref.kind is TypeKind.DATETIME:
        return f"parseDatetime({expr})"
    if type_ref.kind is TypeKind.MODEL:
        return f"{type_ref.name}.fromWire({expr})"
    if type_ref.kind is TypeKind.LIST:
        inner = parse_type(type_ref.arguments[0], "item")
        if inner is None:
            return None
        return f"{expr}.map((item) => {inner})"
    if type_ref.kind is TypeKind.MAP:
        inner = parse_type(type_ref.arguments[0], "value")
        if inner is None:
            return None
        return (
            f"Object.fromEntries(Object.entries({expr})"
            f".map(([key, value]) => [key, {inner}]))"
        )
    return None


def dump_type(type_ref: TypeRef, expr: str) -> str | None:
    """Return a wire encoder expression, or None for identity."""
    if type_ref.kind is TypeKind.DATETIME:
        return f"{expr}.toISOString()"
    if type_ref.kind is TypeKind.MODEL:
        return f"{expr}.toWire()"
    if type_ref.kind is TypeKind.LIST:
        inner = dump_type(type_ref.arguments[0], "item")
        if inner is None:
            return None
        return f"{expr}.map((item) => {inner})"
    if type_ref.kind is TypeKind.MAP:
        inner = dump_type(type_ref.arguments[0], "value")
        if inner is None:
            return None
        return (
            f"Object.fromEntries(Object.entries({expr})"
            f".map(([key, value]) => [key, {inner}]))"
        )
    return None


def discriminator_parse(discriminator: DiscriminatorDef, expr: str) -> str:
    if discriminator.name is not None:
        return (
            f'parse{discriminator.name}(data["{discriminator.parent_field}"], {expr})'
        )
    rendered = parse_type(discriminator.fallback, expr) or expr
    for value, type_ref in reversed(discriminator.cases):
        parsed = parse_type(type_ref, expr) or expr
        rendered = (
            f'data["{discriminator.parent_field}"] === "{value}"'
            f" ? {parsed} : {rendered}"
        )
    return rendered


def discriminator_dump(discriminator: DiscriminatorDef, expr: str) -> str | None:
    variants = [type_ref for _, type_ref in discriminator.cases]
    variants.append(discriminator.fallback)
    rendered = [dump_type(type_ref, expr) for type_ref in variants]
    if rendered and all(value == rendered[0] for value in rendered):
        return rendered[0]
    if any(value is not None for value in rendered):
        return f'(typeof {expr}.toWire === "function" ? {expr}.toWire() : {expr})'
    return None


def field_parse(fld: FieldDef) -> str:
    src = f'data["{fld.wire_name}"]'
    parse = (
        discriminator_parse(fld.discriminator, src)
        if fld.discriminator is not None
        else parse_type(fld.type, src)
    )
    if parse is None:
        return src
    if fld.required and not fld.nullable:
        return parse
    # null and undefined (unset) both pass through untouched
    return f"{src} == null ? {src} : {parse}"


def field_dump(fld: FieldDef) -> list[str]:
    src = f"this.{fld.name}"
    dump = (
        discriminator_dump(fld.discriminator, src)
        if fld.discriminator is not None
        else dump_type(fld.type, src)
    )
    value = src if dump is None else f"{src} === null ? null : {dump}"
    return [
        f"if ({src} !== undefined) {{",
        f'  data["{fld.wire_name}"] = {value};',
        "}",
    ]


def field_ts(fld: FieldDef) -> str:
    base = ts_type(fld.type)
    if fld.nullable and not base.endswith("| null"):
        base = f"{base} | null"
    optional = "?" if not fld.required else ""
    return f"{fld.name}{optional}: {base};"


STREAM_VALUE_METHODS_JS = """
  /** The payload decoded to raw bytes; undecodable base64 becomes
   * empty bytes instead of failing a live stream. */
  asBytes() {
    if (!this.value) {
      return new Uint8Array();
    }
    if (this.encoding === "base64") {
      try {
        return base64ToBytes(this.value);
      } catch {
        return new Uint8Array();
      }
    }
    return textToBytes(this.value);
  }

  /** The payload decoded to text. */
  asText() {
    if (this.encoding === "base64") {
      return bytesToText(this.asBytes());
    }
    return this.value;
  }

  /** Build a payload from raw bytes: printable ASCII stays `ascii`,
   * anything else is base64-encoded - the server-side rule. */
  static fromBytes(value) {
    const printable = value.every(
      (byte) => (byte >= 32 && byte < 127) || byte === 9 || byte === 10 || byte === 13,
    );
    if (printable) {
      return new this({ value: new TextDecoder("ascii").decode(value), encoding: "ascii" });
    }
    return new this({ value: bytesToBase64(value), encoding: "base64" });
  }

  /** Build a payload from text (UTF-8 encoded unless ASCII). */
  static fromText(value) {
    return this.fromBytes(textToBytes(value));
  }
"""

STREAM_VALUE_METHODS_DTS = """
  asBytes(): Uint8Array;
  asText(): string;
  static fromBytes(value: Uint8Array): {name};
  static fromText(value: string): {name};
"""

FILESPEC_MODE_JS = """
    // the wire format is an octal string; accept a plain int too
    if (typeof this.mode === "number") {
      this.mode = this.mode.toString(8).padStart(4, "0");
    }
"""

MODELS_TAIL_JS = """
export const EVENT_DATA_PARSERS = {{
{parsers}
}};

/** Decode a per-type event payload.
 *
 * Unknown event types and payloads that do not match the documented
 * schema - a non-object body included - are returned as-is instead
 * of failing the stream.
 */
export function parseEventData(eventType, data) {{
  if (data === null || typeof data !== "object" || Array.isArray(data)) {{
    return data;
  }}
  const cls = EVENT_DATA_PARSERS[eventType];
  if (cls === undefined) {{
    return data;
  }}
  try {{
    return cls.fromWire(data);
  }} catch {{
    return data;
  }}
}}

/** Decode a stdout/stderr event payload to raw bytes. */
export function decodeChunk(data) {{
  if (data instanceof EventDataStream || data instanceof StreamRepr) {{
    return data.asBytes();
  }}
  if (data === null || typeof data !== "object" || Array.isArray(data)) {{
    return new Uint8Array();
  }}
  const value = data.value ?? "";
  if (typeof value !== "string" || !value) {{
    return new Uint8Array();
  }}
  if (String(data.encoding ?? "ascii") === "base64") {{
    try {{
      return base64ToBytes(value);
    }} catch {{
      return new Uint8Array();
    }}
  }}
  return textToBytes(value);
}}

/** Decode a StreamRepr payload (model or raw object) to a string. */
export function decodeStream(stream) {{
  if (stream instanceof EventDataStream || stream instanceof StreamRepr) {{
    return stream.asText();
  }}
  if (stream === null || typeof stream !== "object" || Array.isArray(stream)) {{
    return "";
  }}
  const value = stream.value ?? "";
  if (typeof value !== "string" || !value) {{
    return "";
  }}
  if (stream.encoding === "base64") {{
    return bytesToText(decodeChunk(stream));
  }}
  return value;
}}
"""


def render_class_js(cls: ModelDef) -> str:
    fields = ordered_fields(cls)
    lines: list[str] = []
    if cls.description:
        lines.append(f"/** {cls.description.splitlines()[0]} */")
    lines.append(f"export class {cls.name} {{")
    lines.append("  constructor(fields = {}) {")
    lines.extend(f"    this.{fld.name} = fields.{fld.name};" for fld in fields)
    if ModelTrait.FILE_MODE in cls.traits:
        lines.append(FILESPEC_MODE_JS.rstrip())
    lines.append("  }")
    lines.append("")
    lines.append("  static fromWire(data) {")
    lines.append(f"    return new {cls.name}({{")
    lines.extend(f"      {fld.name}: {field_parse(fld)}," for fld in fields)
    lines.append("    });")
    lines.append("  }")
    lines.append("")
    lines.append("  toWire() {")
    lines.append("    const data = {};")
    for fld in fields:
        lines.extend(f"    {line}" for line in field_dump(fld))
    lines.append("    return data;")
    lines.append("  }")
    if ModelTrait.STREAM_VALUE in cls.traits:
        lines.append(STREAM_VALUE_METHODS_JS.rstrip())
    lines.append("}")
    return "\n".join(lines)


def render_class_dts(cls: ModelDef) -> str:
    model_fields = ordered_fields(cls)
    fields = [f"  {field_ts(fld)}" for fld in model_fields]
    ctor_fields = " ".join(
        f"{fld.name}?: {ts_type(fld.type)}"
        + (" | null;" if fld.nullable or not fld.required else ";")
        for fld in model_fields
    )
    lines = [f"export declare class {cls.name} {{"]
    lines.extend(fields)
    lines.append(f"  constructor(fields?: {{ {ctor_fields} }});")
    lines.append(f"  static fromWire(data: Record<string, unknown>): {cls.name};")
    lines.append("  toWire(): Record<string, unknown>;")
    if ModelTrait.STREAM_VALUE in cls.traits:
        lines.append(STREAM_VALUE_METHODS_DTS.format(name=cls.name).rstrip())
    lines.append("}")
    return "\n".join(lines)


def render_models_js(ir: SpecIR) -> str:
    statuses = ",\n".join(f'  {value}: "{value}"' for value in ir.status_values)
    terminal = ", ".join(f'"{value}"' for value in ir.terminal_status_values)
    active = ", ".join(
        f'"{value}"'
        for value in ir.status_values
        if value not in ir.terminal_status_values
    )
    event_types = ", ".join(f'"{value}"' for value in ir.event_type_values)
    parsers = "\n".join(
        f"  {event}: {type_ref.name}," for event, type_ref in ir.event_data_variants
    )
    parts = [
        HEADER,
        'import { base64ToBytes, bytesToBase64, bytesToText, parseDatetime, textToBytes } from "./runtime.js";',
        f"export const OPERATION_EVENT_TYPES = Object.freeze([{event_types}]);",
        (
            "/** Operation lifecycle state. */\n"
            f"export const OperationStatus = Object.freeze({{\n{statuses},\n}});"
        ),
        f"export const TERMINAL_STATUSES = new Set([{terminal}]);",
        f"export const ACTIVE_STATUSES = new Set([{active}]);",
        (
            "/** True for statuses that will never change again. */\n"
            "export function isTerminalStatus(status) {\n"
            "  return TERMINAL_STATUSES.has(status);\n"
            "}"
        ),
    ]
    parts.extend(render_class_js(cls) for cls in ir.models)
    parts.append(MODELS_TAIL_JS.format(parsers=parsers).strip())
    return "\n\n".join(parts) + "\n"


def render_models_dts(ir: SpecIR) -> str:
    statuses = " | ".join(f'"{value}"' for value in ir.status_values)
    event_types = " | ".join(f'"{value}"' for value in ir.event_type_values)
    status_consts = " ".join(f'{value}: "{value}";' for value in ir.status_values)
    event_classes = sorted(
        {
            type_ref.name
            for _, type_ref in ir.event_data_variants
            if type_ref.name is not None
        }
    )
    parts = [
        HEADER,
        f"export type OperationEventType = {event_types};",
        "export declare const OPERATION_EVENT_TYPES: readonly OperationEventType[];",
        f"export type OperationStatus = {statuses};",
        f"export declare const OperationStatus: {{ {status_consts} }};",
        "export declare const TERMINAL_STATUSES: Set<OperationStatus>;",
        "export declare const ACTIVE_STATUSES: Set<OperationStatus>;",
        "export declare function isTerminalStatus(status: string): boolean;",
    ]
    parts.extend(render_class_dts(cls) for cls in ir.models)
    union = " | ".join(event_classes)
    parts.append(f"export type EventData = {union};")
    parts.append(
        "export declare const EVENT_DATA_PARSERS: Record<string, { fromWire(data: Record<string, unknown>): EventData }>;"
    )
    parts.append(
        "export declare function parseEventData(eventType: string, data: unknown): EventData | unknown;"
    )
    parts.append(
        "export declare function decodeChunk(data: unknown): Uint8Array;\n"
        "export declare function decodeStream(stream: unknown): string;"
    )
    return "\n\n".join(parts) + "\n"


# ---------------------------------------------------------------------------
# operations.js
# ---------------------------------------------------------------------------


# ECMAScript reserved words cannot be bare locals, but stay legal as
# object keys: the options property keeps the wire name (`case`) while
# destructuring aliases it (`{ case: case_ }`) for use in the body
JS_RESERVED = frozenset({
    "arguments", "await", "break", "case", "catch", "class", "const",
    "continue", "debugger", "default", "delete", "do", "else", "enum",
    "eval", "export", "extends", "false", "finally", "for", "function",
    "if", "implements", "import", "in", "instanceof", "interface",
    "let", "new", "null", "package", "private", "protected", "public",
    "return", "static", "super", "switch", "this", "throw", "true",
    "try", "typeof", "var", "void", "while", "with", "yield",
})  # fmt: skip


def js_local(name: str) -> str:
    return name + "_" if name in JS_RESERVED else name


def js_arg(name: str) -> str:
    """The local identifier of a required (positional) argument.

    Every rendering of a required argument — signatures, call sites,
    .d.ts declarations and doc pages — must agree on this name, so
    they all go through here.
    """
    return js_local(camel(name))


def required_args(op: OperationDef) -> list[str]:
    return [argument.name for argument in op.arguments if argument.required]


def optional_args(op: OperationDef) -> list[tuple[str, str | None]]:
    """(name, js_default) pairs; None means plain destructure (unset)."""
    defaults = {
        ArgumentPresence.OMIT_IF_NULL: "null",
        ArgumentPresence.OMIT_IF_FALSE: "false",
        ArgumentPresence.OMIT_IF_UNSET: None,
    }
    return [
        (argument.name, defaults[argument.presence])
        for argument in op.arguments
        if not argument.required
    ]


def js_params(op: OperationDef, destructure: bool) -> str:
    """The parameter list of a builder/method.

    Required args are positional (camelCase locals); optional args
    arrive in a trailing options object - destructured in builders,
    passed through whole in client methods.
    """
    parts = [js_arg(name) for name in required_args(op)]
    optionals = optional_args(op)
    if optionals:
        if destructure:
            entries = ", ".join(
                (name if js_local(name) == name else f"{name}: {js_local(name)}")
                + ("" if default is None else f" = {default}")
                for name, default in optionals
            )
            parts.append(f"{{ {entries} }} = {{}}")
        else:
            parts.append("options = {}")
    return ", ".join(parts)


def js_reference(op: OperationDef, name: str) -> str:
    """How a builder body refers to the argument *name*."""
    if name in required_args(op):
        return js_arg(name)
    return js_local(name)


def operation_argument(op: OperationDef, name: str) -> ArgumentDef:
    for argument in op.arguments:
        if argument.name == name:
            return argument
    raise KeyError(name)


def argument_ts_type(argument: ArgumentDef) -> str:
    rendered = ts_type(argument.type)
    if argument.nullable:
        rendered += " | null"
    if argument.presence is ArgumentPresence.OMIT_IF_UNSET:
        rendered += " | undefined"
    return rendered


def response_ts_type(op: OperationDef) -> str:
    if op.response.type is None:
        return "null"
    return ts_type(op.response.type)


def response_success_condition(op: OperationDef) -> str:
    if op.response.success is SuccessPolicy.ANY_2XX:
        return "response.status >= 200 && response.status < 300"
    statuses = [
        f"response.status === {status}" for status in op.response.success_statuses
    ]
    if len(statuses) == 1:
        return statuses[0]
    return "(" + " || ".join(statuses) + ")"


def model_names_in_type(type_ref: TypeRef) -> set[str]:
    if type_ref.kind is TypeKind.MODEL:
        assert type_ref.name is not None
        return {type_ref.name}
    names: set[str] = set()
    for argument in type_ref.arguments:
        names.update(model_names_in_type(argument))
    return names


def render_build_fn(op: OperationDef) -> str:
    name = camel(f"build_{op.name}")
    body: list[str] = []
    query = [
        parameter
        for parameter in op.request.parameters
        if parameter.location is ParameterLocation.QUERY
    ]
    headers = [
        parameter
        for parameter in op.request.parameters
        if parameter.location is ParameterLocation.HEADER
    ]
    if query:
        body.append("const query = {};")
        for param in query:
            argument = operation_argument(op, param.argument)
            ref = js_reference(op, param.argument)
            target = f'query["{param.wire_name}"]'
            if param.encoding is ParameterEncoding.ONE_IF_TRUE:
                body.append(f"if ({ref}) {{")
                body.append(f'  {target} = "1";')
                body.append("}")
                continue
            if param.encoding is ParameterEncoding.TIME:
                value = f"formatTimeParam({ref})"
            elif param.encoding is ParameterEncoding.STRING:
                value = f"String({ref})"
            else:
                value = ref
            if argument.required:
                body.append(f"{target} = {value};")
            else:
                body.append(f"if ({ref} != null) {{")
                body.append(f"  {target} = {value};")
                body.append("}")
    if headers:
        body.append("const headers = {};")
        for param in headers:
            ref = js_reference(op, param.argument)
            body.append(f"if ({ref} != null) {{")
            body.append(f'  headers["{param.wire_name}"] = String({ref});')
            body.append("}")
    request_body = op.request.body
    if request_body is not None and request_body.kind is BodyKind.JSON_MODEL:
        ctor = ", ".join(
            name
            if js_reference(op, name) == name
            else f"{name}: {js_reference(op, name)}"
            for name in [argument.name for argument in op.arguments]
            if name != "content"
        )
        assert request_body.model is not None
        assert request_body.model.name is not None
        body.append(
            f"const payload = new {request_body.model.name}({{ {ctor} }}).toWire();"
        )
    elif request_body is not None and request_body.kind is BodyKind.JSON_INLINE:
        body.append("const payload = {};")
        for binding in request_body.bindings:
            argument = operation_argument(op, binding.argument)
            ref = js_reference(op, binding.argument)
            dumped = dump_type(argument.type, ref)
            value = ref if dumped is None else dumped
            if argument.required:
                body.append(f'payload["{binding.wire_name}"] = {value};')
            else:
                body.append(f"if ({ref} !== undefined) {{")
                body.append(f'  payload["{binding.wire_name}"] = {value};')
                body.append("}")
    path = op.path
    for param in op.request.parameters:
        if param.location is ParameterLocation.PATH:
            path = path.replace(
                "{" + param.wire_name + "}",
                "${quotePath(" + js_reference(op, param.argument) + ")}",
            )
    spec_fields = [
        f'method: "{op.http_method}"',
        f"path: `{path}`",
        f"idempotent: {'true' if op.request.idempotent else 'false'}",
    ]
    if query:
        spec_fields.append("query")
    if headers:
        spec_fields.append("headers")
    if request_body is not None and request_body.kind in (
        BodyKind.JSON_MODEL,
        BodyKind.JSON_INLINE,
    ):
        spec_fields.append("body: JSON.stringify(payload)")
        spec_fields.append('contentType: "application/json"')
    elif request_body is not None and request_body.kind is BodyKind.BINARY:
        content = js_reference(op, request_body.bindings[0].argument)
        spec_fields.append(f"body: {content}")
        spec_fields.append('contentType: "application/octet-stream"')
    if op.request.accept is not None:
        spec_fields.append(f'accept: "{op.request.accept}"')
    if op.response.mode is ResponseMode.LOCATION:
        # fetch cannot expose a Location header in browsers (an opaque
        # redirect); follow it and read the final URL instead
        spec_fields.append('redirect: "follow"')
    body.append(f"return {{ {', '.join(spec_fields)} }};")
    lines = [
        f"/** Build the request for `{op.http_method} {op.path}`. */",
        f"export function {name}({js_params(op, destructure=True)}) {{",
        *[f"  {line}" for line in body],
        "}",
    ]
    return "\n".join(lines)


def render_parse_fn(op: OperationDef) -> str | None:
    if op.response.mode in (ResponseMode.SSE, ResponseMode.BYTE_STREAM):
        return None
    name = camel(f"parse_{op.name}")
    success = response_success_condition(op)
    body: list[str] = []
    response = op.response
    if response.mode is ResponseMode.JSON:
        assert response.type is not None
        if response.json_path:
            value = "jsonObject(response)" + "".join(
                f'["{part}"]' for part in response.json_path
            )
            cast = {
                TypeKind.STRING: "String",
                TypeKind.INTEGER: "Number",
                TypeKind.NUMBER: "Number",
                TypeKind.BOOLEAN: "Boolean",
            }.get(response.type.kind)
            parsed = value if cast is None else f"{cast}({value})"
        elif response.type.kind is TypeKind.MODEL:
            parsed = f"{response.type.name}.fromWire(jsonObject(response))"
        elif response.type.kind is TypeKind.LIST:
            item_type = response.type.arguments[0]
            parsed_item = parse_type(item_type, "item") or "item"
            parsed = f"jsonArray(response).map((item) => {parsed_item})"
        else:
            parsed = "jsonObject(response)"
        body += [f"if ({success}) {{", f"  return {parsed};", "}"]
    elif response.mode is ResponseMode.LOCATION:
        assert response.header_name is not None
        exact = response.success_statuses[0]
        body += [
            "// Node exposes the 302 Location; browsers only expose the",
            "// followed response, whose final URL ends with the UUID",
            f'if (response.status === {exact} && response.headers["{response.header_name}"]) {{',
            f'  const location = response.headers["{response.header_name}"];',
            '  return location.replace(/\\/+$/, "").split("/").pop();',
            "}",
            "if ((response.status >= 200 && response.status < 300) && response.url) {",
            "  const path = new URL(response.url).pathname;",
            '  return path.replace(/\\/+$/, "").split("/").pop();',
            "}",
        ]
    elif response.mode is ResponseMode.STATUS_BOOL:
        body += [f"if ({success}) {{", "  return true;", "}"]
    elif response.mode is ResponseMode.BYTES:
        body += [f"if ({success}) {{", "  return response.body;", "}"]
    else:
        assert response.mode is ResponseMode.EMPTY
        body += [f"if ({success}) {{", "  return null;", "}"]
    body.append("throw new RangeError(`unexpected HTTP status ${response.status}`);")
    lines = [
        f"/** Parse the response of `{op.http_method} {op.path}`. */",
        f"export function {name}(response) {{",
        *[f"  {line}" for line in body],
        "}",
    ]
    return "\n".join(lines)


def render_operations_js(ir: SpecIR) -> str:
    parts = [HEADER]
    models: set[str] = set()
    for op in ir.operations:
        request_body = op.request.body
        if request_body is not None and request_body.model is not None:
            assert request_body.model.name is not None
            models.add(request_body.model.name)
        if op.response.mode is ResponseMode.JSON and op.response.type is not None:
            models.update(model_names_in_type(op.response.type))
    imports = ["formatTimeParam", "jsonArray", "jsonObject", "quotePath"]
    parts.append(f'import {{ {", ".join(imports)} }} from "./runtime.js";')
    if models:
        parts.append(f'import {{ {", ".join(sorted(models))} }} from "./models.js";')
    for op in ir.operations:
        parts.append(render_build_fn(op))
        parse = render_parse_fn(op)
        if parse:
            parts.append(parse)
    return "\n\n".join(parts) + "\n"


def option_ts_entries(op: OperationDef) -> str:
    entries = []
    for argument in op.arguments:
        if argument.required:
            continue
        entries.append(f"{argument.name}?: {argument_ts_type(argument)};")
    return " ".join(entries)


def ts_params(op: OperationDef) -> str:
    parts = [
        f"{js_arg(argument.name)}: {argument_ts_type(argument)}"
        for argument in op.arguments
        if argument.required
    ]
    entries = option_ts_entries(op)
    if entries:
        parts.append(f"options?: {{ {entries} }}")
    return ", ".join(parts)


def model_type_names(ir: SpecIR) -> list[str]:
    """Every name models.d.ts exports that other declarations may use."""
    return [
        *ir.model_names,
        "OperationStatus",
        "OperationEventType",
        "EventData",
    ]


def render_operations_dts(ir: SpecIR) -> str:
    decls: list[str] = []
    for op in ir.operations:
        build = camel(f"build_{op.name}")
        decls.append(f"export declare function {build}({ts_params(op)}): RequestSpec;")
        if op.response.mode in (ResponseMode.SSE, ResponseMode.BYTE_STREAM):
            continue
        parse = camel(f"parse_{op.name}")
        decls.append(
            f"export declare function {parse}(response: ResponseData): {response_ts_type(op)};"
        )
    body = "\n\n".join(decls)
    # the import set is computed from the rendered declarations: a
    # missing type import silently degrades the whole surface to `any`
    # under skipLibCheck, so nothing may be left unresolved
    models = used_names(body, model_type_names(ir))
    parts = [
        HEADER,
        'import type { RequestSpec, ResponseData } from "./runtime.js";',
        f'import type {{ {", ".join(models)} }} from "./models.js";',
        body,
    ]
    return "\n\n".join(parts) + "\n"


# ---------------------------------------------------------------------------
# client.js
# ---------------------------------------------------------------------------


CLIENT_HEADER_JS = """
import {{
  APIConnectionError,
  APIStatusError,
  ERROR_CLASSES,
  ServerError,
  NotFoundError,
}} from "./errors.js";
import {{ AUTH_TYPE_IAM, ProfileError, resolveProfile }} from "./profiles.js";
import {{
  IS_NODE,
  SSEParser,
  TIGHT_LOOP_FLOOR,
  UA_PLATFORM,
  UA_PRODUCT,
  UA_RUNTIME,
  decodeFramePayload,
  encodeQuery,
  monotonic,
  responseErrorDetails,
  retryDelays,
  sha256,
  isUuid,
  sleep,
}} from "./runtime.js";
import {{ DEFAULT_BASE_URL }} from "./specInfo.js";
import {{ OperationEvent, isTerminalStatus }} from "./models.js";
import * as operations from "./operations.js";

/** Contree API client on top of the platform fetch.
 *
 * fetch pools keepalive connections and decodes gzip on its own, so
 * unlike the Python adapters there is exactly one transport. Pass a
 * custom `fetch` implementation (undici dispatcher wrappers, MSW,
 * ...) via the constructor options to customize it.
 *
 * `token` may be null, like `project`: the client then sends no
 * `Authorization` header, which is all the endpoints that need no
 * authentication want.
 */
export class ContreeClient {{
  constructor(
    token,
    {{
      baseUrl = DEFAULT_BASE_URL,
      project = null,
      timeout = 300,
      retry = null,
      identity = null,
      fetch: fetchImpl = null,
    }} = {{}},
  ) {{
    // a typo like "htps://" must not silently degrade somewhere else
    const parsed = new URL(baseUrl);
    if (parsed.protocol !== "http:" && parsed.protocol !== "https:") {{
      throw new RangeError(
        `unsupported baseUrl scheme ${{parsed.protocol}} in ${{baseUrl}}: use http:// or https://`,
      );
    }}
    if (parsed.username || parsed.password) {{
      throw new RangeError("baseUrl must not include credentials");
    }}
    this.token = token ?? null;
    this.baseUrl = baseUrl.replace(/\\/+$/, "");
    this.project = project;
    this.timeout = timeout;
    this.retry = retry;
    this.identity = identity;
    this._fetch = fetchImpl ?? globalThis.fetch.bind(globalThis);
  }}

  /** Create a client from a saved Contree profile (Node). */
  static async fromProfile(profile = null, {{ configPath = null, ...options }} = {{}}) {{
    const resolved =
      profile !== null && typeof profile === "object"
        ? profile
        : await resolveProfile(profile, {{ path: configPath }});
    if (!resolved.token || !resolved.token.trim()) {{
      throw new ProfileError(
        `profile ${{JSON.stringify(resolved.name)}} has no token`,
      );
    }}
    if (!resolved.url && resolved.authType !== AUTH_TYPE_IAM) {{
      throw new ProfileError(
        `profile ${{JSON.stringify(resolved.name)}} has no URL`,
      );
    }}
    return new this(resolved.token, {{
      baseUrl: resolved.url || DEFAULT_BASE_URL,
      project: resolved.project ?? null,
      ...options,
    }});
  }}

  /** Compose the User-Agent from the product tokens; the caller's
   * `identity` leads. Browsers forbid sending the header, so it is
   * only attached in Node. */
  userAgent() {{
    return [this.identity, UA_PRODUCT, UA_RUNTIME, UA_PLATFORM]
      .filter(Boolean)
      .join(" ");
  }}

  buildUrl(spec) {{
    let url = `${{this.baseUrl}}/v1${{spec.path}}`;
    if (spec.query && Object.keys(spec.query).length) {{
      url = `${{url}}?${{encodeQuery(spec.query)}}`;
    }}
    return url;
  }}

  buildHeaders(spec) {{
    const headers = {{}};
    if (this.token) {{
      headers["Authorization"] = `Bearer ${{this.token}}`;
    }}
    if (this.project) {{
      headers["Project"] = this.project;
    }}
    if (spec.contentType) {{
      headers["Content-Type"] = spec.contentType;
    }}
    if (spec.accept) {{
      headers["Accept"] = spec.accept;
    }}
    Object.assign(headers, spec.headers ?? {{}});
    if (IS_NODE && !("User-Agent" in headers)) {{
      headers["User-Agent"] = this.userAgent();
    }}
    try {{
      new Headers(headers);
    }} catch {{
      throw new RangeError("invalid HTTP header name or value");
    }}
    return headers;
  }}

  _fetchOptions(spec, signal) {{
    const options = {{
      method: spec.method,
      headers: this.buildHeaders(spec),
      redirect: spec.redirect ?? "manual",
      signal,
    }};
    if (spec.body !== undefined && spec.body !== null) {{
      options.body = spec.body;
      if (typeof ReadableStream !== "undefined" && spec.body instanceof ReadableStream) {{
        options.duplex = "half";
      }}
    }}
    return options;
  }}

  /** Execute one request and map its transport and HTTP errors. */
  async request(spec) {{
    const controller = new AbortController();
    const deadline = spec.deadline ?? null;
    const remaining = deadline === null ? null : deadline - monotonic();
    if (remaining !== null && remaining <= 0) {{
      const cause = new DOMException("request timed out", "TimeoutError");
      throw new APIConnectionError(cause.message, {{ cause, timedOut: true }});
    }}
    const url = this.buildUrl(spec);
    const options = this._fetchOptions(spec, controller.signal);
    const timeout =
      this.timeout === null
        ? remaining
        : remaining === null
          ? this.timeout
          : Math.min(this.timeout, remaining);
    // an explicit timer, not AbortSignal.timeout(): Node unrefs that
    // timer, so it can never fire when nothing else keeps the event
    // loop alive (custom fetch transports, for one)
    const timer =
      timeout !== null
        ? setTimeout(
            () =>
              controller.abort(
                new DOMException("request timed out", "TimeoutError"),
              ),
            Math.ceil(Math.max(0, timeout) * 1000),
          )
        : null;
    try {{
      let response;
      try {{
        response = await this._fetch(url, options);
      }} catch (cause) {{
        if (cause?.name === "AbortError" && !controller.signal.aborted) {{
          throw cause;
        }}
        throw new APIConnectionError(String(cause?.message ?? cause), {{
          cause,
          timedOut:
            cause?.name === "TimeoutError" ||
            controller.signal.reason?.name === "TimeoutError",
        }});
      }}
      if (deadline !== null && monotonic() >= deadline) {{
        const cause = new DOMException("request timed out", "TimeoutError");
        throw new APIConnectionError(cause.message, {{ cause, timedOut: true }});
      }}
      let body;
      try {{
        body = new Uint8Array(await response.arrayBuffer());
      }} catch (cause) {{
        if (cause?.name === "AbortError" && !controller.signal.aborted) {{
          throw cause;
        }}
        throw new APIConnectionError(String(cause?.message ?? cause), {{
          cause,
          timedOut:
            cause?.name === "TimeoutError" ||
            controller.signal.reason?.name === "TimeoutError",
        }});
      }}
      if (deadline !== null && monotonic() >= deadline) {{
        const cause = new DOMException("request timed out", "TimeoutError");
        throw new APIConnectionError(cause.message, {{ cause, timedOut: true }});
      }}
      const headers = {{}};
      response.headers.forEach((value, key) => {{
        headers[key.toLowerCase()] = value;
      }});
      const data = {{ status: response.status, headers, body, url: response.url }};
      if (response.status >= 400) {{
        const {{ error, traceback, retryAfter }} = responseErrorDetails(data);
        const ErrorClass =
          ERROR_CLASSES.get(response.status) ??
          (response.status >= 500 ? ServerError : APIStatusError);
        throw new ErrorClass(response.status, error, {{ traceback, retryAfter }});
      }}
      return data;
    }} finally {{
      if (timer !== null) {{
        clearTimeout(timer);
      }}
    }}
  }}

  /** One transparent reconnect for a replayable connection error:
   * fetch pools keep-alive sockets and cannot pre-check them the way
   * the Python adapters do, so the first request on a socket the
   * server already closed fails without ever reaching the server. */
  async _reconnecting(spec) {{
    try {{
      return await this.request(spec);
    }} catch (error) {{
      if (
        !spec.idempotent ||
        (typeof ReadableStream !== "undefined" &&
          spec.body instanceof ReadableStream) ||
        !(error instanceof APIConnectionError) ||
        error.cause?.name !== "TypeError"
      ) {{
        throw error;
      }}
      return await this.request(spec);
    }}
  }}

  /** Execute a buffered request, retrying per the client policy. */
  async call(spec) {{
    const policy = this.retry;
    if (policy === null) {{
      return await this._reconnecting(spec);
    }}
    if (
      typeof ReadableStream !== "undefined" &&
      spec.body instanceof ReadableStream
    ) {{
      // a stream cannot be replayed: single attempt
      return await this.request(spec);
    }}
    // a lost response after a non-idempotent request (POST) could
    // mean a second execution server-side: never blind-retry unless
    // the caller explicitly opted into that risk. 425 Too Early and
    // 429 Too Many Requests are the exceptions - the backend's
    // contract guarantees both mean the request was rejected before
    // any processing, so replaying is always safe.
    const replaySafe = spec.idempotent || policy.retryUnsafe;
    const deadline = spec.deadline ?? null;
    const delays = retryDelays(policy.delays);
    let attempts = 0;
    for (;;) {{
      attempts += 1;
      const exhausted =
        policy.maxAttempts !== null && attempts >= policy.maxAttempts;
      try {{
        return await this.request(spec);
      }} catch (error) {{
        const retryableStatus =
          error instanceof APIStatusError && policy.retryableStatus(error.status);
        const rejectedBeforeProcessing =
          error instanceof APIStatusError &&
          (error.status === 425 || error.status === 429);
        if (
          !(error instanceof APIConnectionError) && !retryableStatus ||
          (!replaySafe && !rejectedBeforeProcessing) ||
          exhausted ||
          (deadline !== null && monotonic() >= deadline)
        ) {{
          throw error;
        }}
        let delay =
          error instanceof APIStatusError && error.retryAfter !== null
            ? error.retryAfter
            : delays.next().value;
        if (deadline !== null) {{
          delay = Math.min(delay, Math.max(0, deadline - monotonic()));
        }}
        await sleep(delay);
      }}
    }}
  }}

  /** Execute the request and yield response body chunks. */
  async *stream(spec) {{
    let controller;
    let timer = null;
    const disarm = () => {{
      if (timer !== null) {{
        clearTimeout(timer);
        timer = null;
      }}
    }};
    const abortIn = (seconds) => {{
      disarm();
      if (seconds === null) {{
        return;
      }}
      timer = setTimeout(
        () =>
          controller.abort(
            new DOMException("stream read timed out", "TimeoutError"),
          ),
        Math.ceil(Math.max(0, seconds) * 1000),
      );
    }};
    const sse = spec.accept === "text/event-stream";
    const deadline = spec.deadline ?? null; // monotonic seconds
    // the budget for the next transport wait: downloads use the
    // client timeout as an idle cap, bounded by the caller's absolute
    // deadline; SSE is unbounded unless the caller set a deadline.
    // Keepalive frames re-enter abortIn with a shrinking remainder, so
    // the bound stays absolute while the server keeps the stream warm.
    const nextBudget = () => {{
      const remaining = deadline === null ? null : deadline - monotonic();
      if (sse || this.timeout === null) {{
        return remaining;
      }}
      return remaining === null
        ? this.timeout
        : Math.min(this.timeout, remaining);
    }};
    const connectBudget = () => {{
      const remaining = deadline === null ? null : deadline - monotonic();
      if (this.timeout === null) {{
        return remaining;
      }}
      return remaining === null
        ? this.timeout
        : Math.min(this.timeout, remaining);
    }};
    // the server may close a pooled keep-alive socket between
    // requests and fetch exposes no pool to pre-check (the Python
    // adapters validate pooled connections instead): a replayable
    // connect that fails on the transport is retried per the client
    // policy, and even with no policy it gets one transparent
    // reconnect - the body has not been consumed yet, so it is safe
    const replayable =
      spec.idempotent &&
      !(
        typeof ReadableStream !== "undefined" &&
        spec.body instanceof ReadableStream
      );
    // SSE reconnection belongs to followOperationEvents(), which
    // checks terminal status after each failed stream.
    const policy = replayable && !sse ? this.retry : null;
    const maxAttempts =
      policy !== null ? policy.maxAttempts : replayable ? 2 : 1;
    const delays = policy === null ? null : retryDelays(policy.delays);
    let attempts = 0;
    let response;
    for (;;) {{
      if (deadline !== null && monotonic() >= deadline) {{
        throw new DOMException("stream read timed out", "TimeoutError");
      }}
      attempts += 1;
      controller = new AbortController();
      abortIn(connectBudget());
      try {{
        response = await this._fetch(
          this.buildUrl(spec),
          this._fetchOptions(spec, controller.signal),
        );
        if (deadline !== null && monotonic() >= deadline) {{
          throw new DOMException("stream read timed out", "TimeoutError");
        }}
        break;
      }} catch (error) {{
        disarm();
        const reconnectable =
          error?.name === "TypeError" ||
          (policy !== null && error?.name === "TimeoutError");
        if (
          !reconnectable ||
          (maxAttempts !== null && attempts >= maxAttempts) ||
          (deadline !== null && monotonic() >= deadline)
        ) {{
          throw error;
        }}
        let delay = delays === null ? 0 : delays.next().value;
        if (deadline !== null) {{
          delay = Math.min(delay, Math.max(0, deadline - monotonic()));
        }}
        await sleep(delay);
      }}
    }}
    disarm();
    const reader = response.body.getReader();
    try {{
      if (response.status >= 400) {{
        throw new RangeError(`HTTP ${{response.status}}`);
      }}
      for (;;) {{
        const budget = nextBudget();
        if (budget !== null && budget <= 0) {{
          throw new DOMException("stream read timed out", "TimeoutError");
        }}
        abortIn(budget);
        const {{ done, value }} = await reader.read();
        // the timer covers only the transport wait, never the
        // consumer's processing of the yielded chunk
        disarm();
        if (deadline !== null && monotonic() >= deadline) {{
          throw new DOMException("stream read timed out", "TimeoutError");
        }}
        if (done) {{
          return;
        }}
        yield value;
      }}
    }} finally {{
      disarm();
      try {{
        await reader.cancel();
      }} catch {{
        // the stream is already gone; releasing is best-effort
      }}
    }}
  }}

  async open() {{}}

  async close() {{}}

  /** Best-effort check that the operation reached a terminal state. */
  async operationTerminal(operationId, deadline = null) {{
    let status;
    const deadlineLimited =
      deadline !== null &&
      (this.timeout === null || deadline - monotonic() <= this.timeout);
    try {{
      // The outer event loop owns retries. Each status probe uses one
      // transport attempt and stays inside the caller's deadline.
      const spec = operations.buildGetOperationStatus(operationId);
      spec.deadline = deadline;
      status = operations.parseGetOperationStatus(await this.request(spec)).status;
    }} catch (error) {{
      if (error instanceof APIConnectionError) {{
        if (
          deadline !== null &&
          (monotonic() >= deadline || (deadlineLimited && error.timedOut))
        ) {{
          throw new DOMException(
            `operation ${{operationId}} status probe exceeded its deadline`,
            "TimeoutError",
          );
        }}
        return false;
      }}
      if (error instanceof APIStatusError) {{
        if (
          error.status === 410 ||
          error.status === 425 ||
          error.status === 429 ||
          error.status >= 500
        ) {{
          return false;
        }}
      }}
      throw error;
    }}
    return status !== undefined && isTerminalStatus(status);
  }}

  /** Wait until the operation finishes, driven by its event stream:
   * follows the SSE log until `completion`, then fetches and returns
   * the terminal OperationResponse. */
  async waitOperation(operationId, {{ timeout = null }} = {{}}) {{
    const deadline = timeout === null ? null : monotonic() + timeout;
    // eslint-disable-next-line no-unused-vars
    for await (const event of this.followOperationEvents(operationId, {{ timeout }})) {{
      // draining the stream is the wait
    }}
    const spec = operations.buildGetOperationStatus(operationId);
    spec.deadline = deadline;
    const deadlineLimited =
      deadline !== null &&
      (this.timeout === null || deadline - monotonic() <= this.timeout);
    try {{
      return operations.parseGetOperationStatus(await this.call(spec));
    }} catch (error) {{
      if (
        deadline !== null &&
        error instanceof APIConnectionError &&
        (monotonic() >= deadline || (deadlineLimited && error.timedOut))
      ) {{
        throw new DOMException(
          `operation ${{operationId}} did not complete within ${{timeout}}s`,
          "TimeoutError",
        );
      }}
      throw error;
    }}
  }}

  /** Stream operation events with transparent reconnection.
   * Native stream failures trigger a terminal-status probe and a
   * reconnect from the last event id. */
  async *followOperationEvents(
    operationId,
    {{ last_event_id = null, spid = null, since = null, timeout = null }} = {{}},
  ) {{
    let lastId = last_event_id;
    const deadline = timeout === null ? null : monotonic() + timeout;
    const checkDeadline = () => {{
      if (deadline !== null && monotonic() >= deadline) {{
        throw new DOMException(
          `operation ${{operationId}} events did not complete within ${{timeout}}s`,
          "TimeoutError",
        );
      }}
    }};
    for (;;) {{
      checkDeadline();
      const eventsBefore = lastId;
      try {{
        for await (const event of this.iterOperationEvents(operationId, {{
          follow: true,
          spid,
          since,
          last_event_id: lastId,
          deadline,
        }})) {{
          lastId = event.id;
          yield event;
          if (event.type === "completion") {{
            return;
          }}
          checkDeadline();
        }}
      }} catch (error) {{
        if (error?.name === "AbortError") {{
          throw error;
        }}
        checkDeadline();
        if (Number.isInteger(error?.lastEventId)) {{
          lastId = error.lastEventId;
        }}
      }}
      // the stream ended or broke without a completion frame: the
      // retry must not outlive the operation itself
      if (await this.operationTerminal(operationId, deadline)) {{
        return;
      }}
      if (lastId === eventsBefore) {{
        let delay = TIGHT_LOOP_FLOOR;
        if (deadline !== null) {{
          delay = Math.min(delay, Math.max(0, deadline - monotonic()));
        }}
        await sleep(delay);
      }}
    }}
  }}

  /** Resolve an image reference (UUID, `tag:NAME` or bare tag) to a UUID. */
  async resolveImage(ref) {{
    if (ref.startsWith("tag:")) {{
      return await this.inspectFindImageByTag(ref.slice(4));
    }}
    if (isUuid(ref)) {{
      return ref;
    }}
    return await this.inspectFindImageByTag(ref);
  }}

  /** Upload *content* unless the server already stores it (sha256
   * dedup). A ReadableStream cannot be hashed and replayed, so
   * without a caller-provided sha256 it uploads directly. */
  async ensureFile(content, {{ sha256: digest = null }} = {{}}) {{
    const resolved = digest !== null ? digest : await sha256(content);
    if (resolved === null) {{
      return await this.uploadFile(content);
    }}
    try {{
      return await this.getFile(resolved);
    }} catch (error) {{
      if (error instanceof NotFoundError) {{
        return await this.uploadFile(content);
      }}
      throw error;
    }}
  }}
"""

ITER_METHOD_JS = """
  /** Iterate over {list_method}() results across pages; offset
   * pagination is transparent, breaking out stops fetching. */
  async *{name}(options = {{}}) {{
    const {{ page_size = {page_max}, limit = null, ...filters }} = options;
    if (!Number.isInteger(page_size) || page_size < 1 || page_size > {page_max}) {{
      throw new RangeError("page_size must be an integer between 1 and {page_max}");
    }}
    if (limit !== null && (!Number.isInteger(limit) || limit < 0)) {{
      throw new RangeError("limit must be null or a non-negative integer");
    }}
    let fetched = 0;
    let offset = 0;
    for (;;) {{
      const size = limit === null ? page_size : Math.min(page_size, limit - fetched);
      if (size <= 0) {{
        return;
      }}
      const response = await this.{list_method}({{ ...filters, limit: size, offset }});
      const page = {page_expr} ?? [];
      if (!page.length) {{
        return;
      }}
      for (const item of page) {{
        yield item;
        fetched += 1;
        if (limit !== null && fetched >= limit) {{
          return;
        }}
      }}
      if (page.length < size) {{
        return;
      }}
      offset += page.length;
    }}
  }}
"""


def method_call_args(op: OperationDef) -> str:
    parts = [js_arg(name) for name in required_args(op)]
    if optional_args(op):
        parts.append("options")
    return ", ".join(parts)


def render_client_method(op: OperationDef) -> list[str]:
    name = camel(op.name)
    build = camel(f"build_{op.name}")
    parse = camel(f"parse_{op.name}")
    params = js_params(op, destructure=False)
    call_args = method_call_args(op)
    doc = f"  /** {op.summary or op.name} ({op.http_method} {op.path}) */"
    blocks: list[str] = []
    mode = op.response.mode
    if mode not in (ResponseMode.SSE, ResponseMode.BYTE_STREAM):
        if mode is ResponseMode.STATUS_BOOL:
            blocks.append(
                f"{doc}\n"
                f"  async {name}({params}) {{\n"
                f"    const spec = operations.{build}({call_args});\n"
                f"    try {{\n"
                f"      return operations.{parse}(await this.call(spec));\n"
                f"    }} catch (error) {{\n"
                f"      if (error instanceof NotFoundError) {{\n"
                f"        return false;\n"
                f"      }}\n"
                f"      throw error;\n"
                f"    }}\n"
                f"  }}"
            )
        else:
            blocks.append(
                f"{doc}\n"
                f"  async {name}({params}) {{\n"
                f"    const spec = operations.{build}({call_args});\n"
                f"    return operations.{parse}(await this.call(spec));\n"
                f"  }}"
            )
        if mode is ResponseMode.BYTES:
            stream_name = camel(f"{op.name}_stream")
            blocks.append(
                f"  /** Streaming variant of {name}(). */\n"
                f"  async *{stream_name}({params}) {{\n"
                f"    const spec = operations.{build}({call_args});\n"
                f"    yield* this.stream(spec);\n"
                f"  }}"
            )
    elif mode is ResponseMode.BYTE_STREAM:
        blocks.append(
            f"{doc}\n"
            f"  async *{name}({params}) {{\n"
            f"    const spec = operations.{build}({call_args});\n"
            f"    yield* this.stream(spec);\n"
            f"  }}"
        )
    elif mode is ResponseMode.SSE:
        resume_argument = op.response.resume_argument
        assert resume_argument is not None
        event_type = op.response.type
        assert event_type is not None and event_type.name is not None
        blocks.append(
            f"{doc}\n"
            f"  async *{name}({params}) {{\n"
            f"    const spec = operations.{build}({call_args});\n"
            f"    if (options.deadline != null) {{{{\n"
            f"      spec.deadline = options.deadline;\n"
            f"    }}}}\n"
            f"    const parser = new SSEParser();\n"
            f"    let lastSeen = options.{resume_argument} ?? null;\n"
            f"    for await (const chunk of this.stream(spec)) {{\n"
            f"      for (const frame of parser.feed(chunk)) {{\n"
            f"        if (frame.id !== null) {{\n"
            f"          lastSeen = frame.id;\n"
            f"        }}\n"
            f"        const payload = decodeFramePayload(frame, lastSeen);\n"
            f"        if (payload === null) {{\n"
            f"          continue;\n"
            f"        }}\n"
            f"        yield {event_type.name}.fromWire(payload);\n"
            f"      }}\n"
            f"    }}\n"
            f"  }}"
        )
    return blocks


def render_client_js(ir: SpecIR) -> str:
    parts = [HEADER, CLIENT_HEADER_JS.strip("\n").format()]
    methods: list[str] = []
    for op in ir.operations:
        methods.extend(render_client_method(op))
        pagination = op.pagination
        if pagination is not None:
            page_expr = "response" + "".join(
                f".{part}" for part in pagination.items_path
            )
            methods.append(
                ITER_METHOD_JS.strip("\n").format(
                    name=camel(pagination.iterator_name),
                    list_method=camel(op.name),
                    page_expr=page_expr,
                    page_max=pagination.max_page_size,
                )
            )
    body = "\n\n".join(methods)
    return f"{parts[0]}\n{parts[1]}\n\n{body}\n}}\n"


def client_method_dts(op: OperationDef) -> list[str]:
    name = camel(op.name)
    params = ts_params(op)
    lines: list[str] = []
    mode = op.response.mode
    if mode not in (ResponseMode.SSE, ResponseMode.BYTE_STREAM):
        lines.append(f"  {name}({params}): Promise<{response_ts_type(op)}>;")
        if mode is ResponseMode.BYTES:
            lines.append(
                f"  {camel(op.name + '_stream')}({params}): AsyncGenerator<Uint8Array>;"
            )
    elif mode is ResponseMode.BYTE_STREAM:
        lines.append(f"  {name}({params}): AsyncGenerator<Uint8Array>;")
    elif mode is ResponseMode.SSE:
        assert op.response.type is not None
        lines.append(
            f"  {name}({params}): AsyncGenerator<{ts_type(op.response.type)}>;"
        )
    return lines


def type_contains(type_ref: TypeRef, kinds: set[TypeKind]) -> bool:
    return type_ref.kind in kinds or any(
        type_contains(argument, kinds) for argument in type_ref.arguments
    )


def pagination_method_dts(op: OperationDef) -> list[str]:
    pagination = op.pagination
    assert pagination is not None
    excluded = {pagination.limit_argument, pagination.offset_argument}
    filters = [argument for argument in op.arguments if argument.name not in excluded]
    lines = [f"  {camel(pagination.iterator_name)}(options?: {{"]
    for argument in filters:
        if type_contains(argument.type, {TypeKind.ENUM, TypeKind.LITERAL}):
            value_type = "string" + (" | null" if argument.nullable else "")
        else:
            value_type = argument_ts_type(argument)
        lines.append(f"    {argument.name}?: {value_type};")
    lines.extend(
        [
            "    page_size?: number;",
            "    limit?: number | null;",
            f"  }}): AsyncGenerator<{ts_type(pagination.item_type)}>;",
        ]
    )
    return lines


CLIENT_DTS_HEADER = """
export interface ContreeClientOptions {
  baseUrl?: string;
  project?: string | null;
  timeout?: number | null;
  retry?: RetryPolicy | null;
  identity?: string | null;
  fetch?: typeof fetch | null;
}

export declare class ContreeClient {
  token: string | null;
  baseUrl: string;
  project: string | null;
  timeout: number | null;
  retry: RetryPolicy | null;
  identity: string | null;
  constructor(token: string | null, options?: ContreeClientOptions);
  static fromProfile(
    profile?: string | Profile | null,
    options?: ContreeClientOptions & { configPath?: string | null },
  ): Promise<ContreeClient>;
  userAgent(): string;
  buildUrl(spec: RequestSpec): string;
  buildHeaders(spec: RequestSpec): Record<string, string>;
  request(spec: RequestSpec): Promise<ResponseData>;
  call(spec: RequestSpec): Promise<ResponseData>;
  stream(spec: RequestSpec): AsyncGenerator<Uint8Array>;
  open(): Promise<void>;
  close(): Promise<void>;
  operationTerminal(operationId: string): Promise<boolean>;
  waitOperation(
    operationId: string,
    options?: { timeout?: number | null },
  ): Promise<OperationResponse>;
  followOperationEvents(
    operationId: string,
    options?: {
      last_event_id?: number | null;
      spid?: number | null;
      since?: number | null;
      timeout?: number | null;
    },
  ): AsyncGenerator<OperationEvent>;
  resolveImage(ref: string): Promise<string>;
  ensureFile(
    content: Uint8Array | string | Blob | ReadableStream<Uint8Array>,
    options?: { sha256?: string | null },
  ): Promise<FileResponse | File>;
"""


def render_client_dts(ir: SpecIR) -> str:
    lines = [CLIENT_DTS_HEADER.strip("\n")]
    model_order = {name: index for index, name in enumerate(ir.model_names)}
    paginated = [op for op in ir.operations if op.pagination is not None]
    paginated.sort(
        key=lambda op: (
            model_order.get(op.pagination.item_type.name or "", len(model_order))
            if op.pagination is not None
            else len(model_order)
        )
    )
    for op in paginated:
        lines.extend(pagination_method_dts(op))
    for op in ir.operations:
        lines.extend(client_method_dts(op))
    lines.append("}")
    body = "\n".join(lines)
    # computed, not hand-listed: an unresolved type in a declaration
    # silently becomes `any` under skipLibCheck
    models = used_names(body, model_type_names(ir))
    imports = "\n".join(
        [
            'import type { Profile } from "./profiles.js";',
            "import type {",
            "  RequestSpec,",
            "  ResponseData,",
            "  RetryPolicy,",
            '} from "./runtime.js";',
            "import type {",
            *[f"  {name}," for name in models],
            '} from "./models.js";',
        ]
    )
    return f"{HEADER}\n{imports}\n\n{body}\n"


# ---------------------------------------------------------------------------
# specInfo / index
# ---------------------------------------------------------------------------


def render_spec_info_js(ir: SpecIR) -> str:
    return (
        f"{HEADER}\n"
        f'export const DEFAULT_BASE_URL = "{ir.default_base_url}";\n\n'
        "// sha256 of the exact OpenAPI document this package was built\n"
        "// from - the build input provenance\n"
        f'export const SPEC_SHA256 = "{ir.spec_sha256}";\n'
    )


def render_spec_info_dts(ir: SpecIR) -> str:
    return (
        f"{HEADER}\n"
        "export declare const DEFAULT_BASE_URL: string;\n"
        "export declare const SPEC_SHA256: string;\n"
    )


# ---------------------------------------------------------------------------
# docs/js/reference.rst
# ---------------------------------------------------------------------------


def rst_field(name: str, body: str) -> list[str]:
    """One wrapped reST field-list entry (`:param x: ...`)."""
    text = " ".join(body.split())
    return restore_literals(
        textwrap.wrap(
            protect_literals(f":{name}: {text}"),
            width=72,
            subsequent_indent="   ",
            break_long_words=False,
            break_on_hyphens=False,
        )
    )


def indented(lines: list[str], pad: str = "   ") -> list[str]:
    return [f"{pad}{line}".rstrip() for line in lines]


def op_signature(op: OperationDef) -> str:
    parts = [js_arg(name) for name in required_args(op)]
    if optional_args(op):
        parts.append("options?")
    return ", ".join(parts)


def op_returns(op: OperationDef) -> str:
    if op.response.mode is ResponseMode.SSE:
        assert op.response.type is not None
        return f"AsyncGenerator<{ts_type(op.response.type)}>"
    if op.response.mode is ResponseMode.BYTE_STREAM:
        return "AsyncGenerator<Uint8Array>"
    return f"Promise<{response_ts_type(op)}>"


def render_op_reference(op: OperationDef) -> str:
    """A js-domain method definition: Sphinx and the Mintlify writer
    render these with the same machinery as the Python autodoc pages
    (signatures, ParamField/ResponseField, anchors)."""
    lines = [f".. js:method:: ContreeClient.{camel(op.name)}({op_signature(op)})", ""]
    body: list[str] = [
        f"``{op.http_method} {op.path}`` — "
        + " ".join(sanitize_doc(op.summary or op.name, escape=False).split())
    ]
    if op.description:
        body.append("")
        body.extend(doc_block_lines(op.description, escape=False))
    body.append("")
    for argument in op.arguments:
        if argument.required:
            name = f"param {js_arg(argument.name)}"
        else:
            name = f"param options.{argument.name}"
        raw_doc = documentation_text(
            argument.documentation.description,
            argument.documentation.example,
            argument.documentation.has_example,
        )
        doc = " ".join(sanitize_doc(raw_doc, escape=False).split())
        text = f"``{argument_ts_type(argument)}``" + (f" — {doc}" if doc else "")
        body.extend(rst_field(name, text))
    body.extend(rst_field("returns", f"``{op_returns(op)}``"))
    lines.extend(indented(body))
    if op.response.mode is ResponseMode.BYTES:
        stream_sig = ", ".join(js_arg(name) for name in required_args(op))
        stream_name = camel(f"{op.name}_stream")
        lines.append("")
        lines.append(f".. js:method:: ContreeClient.{stream_name}({stream_sig})")
        lines.append("")
        lines.extend(
            indented(
                [
                    f"Streaming variant of :js:meth:`ContreeClient.{camel(op.name)}`.",
                    "",
                    *rst_field("returns", "``AsyncGenerator<Uint8Array>``"),
                ]
            )
        )
    return "\n".join(lines)


def render_model_reference(cls: ModelDef) -> str:
    lines = [f".. js:class:: {cls.name}(fields?)", ""]
    body: list[str] = []
    if cls.description:
        body.extend(doc_block_lines(cls.description, escape=False))
        body.append("")
    for fld in ordered_fields(cls):
        base = ts_type(fld.type)
        if fld.nullable and not base.endswith("| null"):
            base = f"{base} | null"
        body.append(f".. js:attribute:: {cls.name}.{fld.name}")
        body.append("")
        marker = "required" if fld.required else "optional"
        raw_doc = documentation_text(
            fld.documentation.description,
            fld.documentation.example,
            fld.documentation.has_example,
        )
        doc = " ".join(sanitize_doc(raw_doc, escape=False).split())
        field_line = f"``{base}`` ({marker})" + (f" — {doc}" if doc else "")
        body.extend(
            indented(
                restore_literals(
                    textwrap.wrap(
                        protect_literals(field_line),
                        width=69,
                        break_long_words=False,
                        break_on_hyphens=False,
                    )
                )
            )
        )
        body.append("")
    if ModelTrait.STREAM_VALUE in cls.traits:
        body.append(
            f"Codec helpers: ``asBytes()``, ``asText()``,"
            f" ``{cls.name}.fromBytes(bytes)``, ``{cls.name}.fromText(text)``."
        )
        body.append("")
    lines.extend(indented(body))
    return "\n".join(lines).rstrip()


REFERENCE_HELPERS_RST = """\
.. js:method:: ContreeClient.waitOperation(operationId, options?)

   Follows the event stream to the ``completion`` frame and fetches
   the terminal ``OperationResponse`` — push, no polling.

   :param operationId: ``string``
   :param options.timeout: ``number | null`` — bounds the whole wait
      in seconds, idle streams included; expiry rejects with a
      ``TimeoutError`` ``DOMException``.
   :returns: ``Promise<OperationResponse>``

.. js:method:: ContreeClient.followOperationEvents(operationId, options?)

   :js:meth:`ContreeClient.iterOperationEvents` with ``follow: true``
   and transparent reconnection: network drops, transport timeouts,
   in-band ``sse_error`` frames and retryable statuses (410/425/5xx)
   resume from the last received event id; other API errors propagate.
   Ends after the ``completion`` frame.

   :param operationId: ``string``
   :param options.last_event_id: ``number | null``
   :param options.spid: ``number | null``
   :param options.since: ``number | null``
   :param options.timeout: ``number | null`` — overall deadline in
      seconds; expiry throws a ``TimeoutError`` ``DOMException``.
   :returns: ``AsyncGenerator<OperationEvent>``

.. js:method:: ContreeClient.resolveImage(ref)

   Resolves a UUID, ``tag:NAME`` or bare tag to the image UUID.

   :param ref: ``string``
   :returns: ``Promise<string>``

.. js:method:: ContreeClient.ensureFile(content, options?)

   Uploads *content* unless the server already stores it: the sha256
   (``options.sha256`` when known, computed locally otherwise) is
   probed via :js:meth:`ContreeClient.getFile`, only a miss uploads.
   A ``ReadableStream`` cannot be hashed and replayed, so it skips
   deduplication.

   :param content: ``Uint8Array | string | Blob | ReadableStream<Uint8Array>``
   :param options.sha256: ``string | null``
   :returns: ``Promise<FileResponse | File>``

.. js:method:: ContreeClient.iterImages(options?)

   Lazy offset pagination over
   :js:meth:`ContreeClient.listImages`: the listing filters plus
   ``page_size`` (server-capped) and ``limit`` (total records;
   ``null`` iterates everything). Breaking out of the loop stops
   fetching. ``iterOperations()`` and ``iterFiles()`` mirror it for
   their listings.

   :returns: ``AsyncGenerator<Image>``
"""


def render_reference(ir: SpecIR) -> str:
    heading = "API reference"
    parts = [
        f"{heading}\n{'=' * len(heading)}",
        f".. {GENERATED_NOTE}",
        (
            "Generated from the OpenAPI specification"
            f" (SHA-256 ``{ir.spec_sha256[:12]}...``). Methods are camelCase;"
            " model fields and option keys keep their snake_case wire"
            " spelling. Required arguments are positional, optional ones"
            " ride in a trailing ``options`` object."
        ),
        "Client methods\n--------------",
    ]
    parts.extend(render_op_reference(op) for op in ir.operations)
    parts.append("Helpers\n-------")
    parts.append(REFERENCE_HELPERS_RST.rstrip())
    statuses = ", ".join(f"``{value}``" for value in ir.status_values)
    terminal = ", ".join(f"``{value}``" for value in ir.terminal_status_values)
    events = ", ".join(f"``{value}``" for value in ir.event_type_values)
    parts.append(
        "Statuses and events\n-------------------\n\n"
        f"Operation statuses are wire strings: {statuses}. Terminal:"
        f" {terminal} — membership via ``TERMINAL_STATUSES`` /"
        " ``ACTIVE_STATUSES`` sets or ``isTerminalStatus(status)``."
        f"\n\nEvent types: {events}; each maps to its ``EventData*``"
        " payload class below, unknown payloads stay plain objects"
        " (``parseEventData``)."
    )
    parts.append("Models\n------")
    parts.extend(render_model_reference(cls) for cls in ir.models)
    return "\n\n".join(parts) + "\n"


INDEX_JS = """
export * from "./errors.js";
export * from "./specInfo.js";
export * from "./models.js";
export {
  CHUNK_SIZE,
  PACKAGE_VERSION,
  RETRY_DELAYS,
  RetryPolicy,
  SSEParser,
  base64ToBytes,
  bytesToBase64,
  bytesToText,
  parseDatetime,
  parseRetryAfter,
  sha256,
  textToBytes,
} from "./runtime.js";
export { ContreeClient } from "./client.js";
export * as operations from "./operations.js";
export * as profiles from "./profiles.js";
export * as testing from "./testing.js";
"""

INDEX_DTS = """
export * from "./errors.js";
export * from "./specInfo.js";
export * from "./models.js";
export {
  CHUNK_SIZE,
  PACKAGE_VERSION,
  RETRY_DELAYS,
  RetryPolicy,
  SSEParser,
  base64ToBytes,
  bytesToBase64,
  bytesToText,
  parseDatetime,
  parseRetryAfter,
  sha256,
  textToBytes,
} from "./runtime.js";
export type { RequestSpec, ResponseData, RequestBody } from "./runtime.js";
export { ContreeClient, ContreeClientOptions } from "./client.js";
export * as operations from "./operations.js";
export * as profiles from "./profiles.js";
export * as testing from "./testing.js";
"""


class JsEmitter(Emitter):
    """Renders the contree-client npm package; gated by prettier and
    a `node --check` syntax pass."""

    files = GENERATED_FILES

    def render(self, ir: SpecIR) -> dict[str, str]:
        # the docs reference page rides along: rendered from the same
        # IR, published by generate() next to the package
        self.reference = render_reference(ir)
        return {
            "models.js": render_models_js(ir),
            "models.d.ts": render_models_dts(ir),
            "operations.js": render_operations_js(ir),
            "operations.d.ts": render_operations_dts(ir),
            "client.js": render_client_js(ir),
            "client.d.ts": render_client_dts(ir),
            "specInfo.js": render_spec_info_js(ir),
            "specInfo.d.ts": render_spec_info_dts(ir),
            "index.js": HEADER + INDEX_JS.lstrip("\n"),
            "index.d.ts": HEADER + INDEX_DTS.lstrip("\n"),
        }

    def validate(self, paths: list[Path]) -> None:
        node = shutil.which("node")
        if node is None:
            raise RuntimeError("node is required to validate the generated JavaScript")
        # staging lives inside the package dir: node_modules sits two
        # levels up (client-js/node_modules); prettier is best-effort
        # here - `make lint-js` is the hard formatting/tsc gate
        package_root = paths[0].parent.parent
        # shutil.which resolves the platform launcher (prettier.CMD on
        # Windows - executing the extensionless POSIX shim there dies
        # with WinError 193)
        prettier = shutil.which(
            "prettier", path=str(package_root / "node_modules" / ".bin")
        )
        if prettier is not None:
            subprocess.run(
                [prettier, "--write", *[str(path) for path in paths]],
                check=True,
            )
        for path in paths:
            if path.suffix == ".js":
                subprocess.run([node, "--check", str(path)], check=True)

    def generate(self, spec_source: str | Path, package_dir: Path) -> Path:
        result = super().generate(spec_source, package_dir)
        # publish the API reference into the docs tree when it exists
        # (generation into a temporary package skips it)
        target = reference_target(package_dir)
        if target.parent.is_dir():
            staging = target.with_suffix(".rst.tmp")
            staging.write_text(self.reference, encoding="utf-8")
            os.replace(staging, target)
        return result


def generate(spec_source: str | Path, package_dir: Path) -> Path:
    """Generate the JavaScript contree-client package."""
    return JsEmitter().generate(spec_source, package_dir)


def reference_target(package_dir: Path) -> Path:
    """docs/js/reference.rst next to the package (repo layout)."""
    return package_dir.parent.parent / "docs" / "js" / "reference.rst"
