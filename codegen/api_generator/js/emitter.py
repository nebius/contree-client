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

from api_generator.emitter import GENERATED_NOTE, Emitter
from api_generator.ir import (
    ClassDef,
    FieldDef,
    OpDef,
    SpecIR,
    doc_block_lines,
    protect_literals,
    restore_literals,
    sanitize_doc,
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

EVENT_DATA_CLASSES = {
    "init": "EventDataInit",
    "spawn": "EventDataSpawn",
    "stdin": "EventDataStream",
    "stdout": "EventDataStream",
    "stderr": "EventDataStream",
    "exit": "EventDataExit",
    "truncated": "EventDataTruncated",
    "size_cap": "EventDataSizeCap",
    "network": "EventDataNetwork",
    "shutdown": "EventDataShutdown",
    "completion": "EventDataCompletion",
}

ITER_LIST_OPERATIONS = {
    "list_images": ("iter_images", "images", "Image"),
    "list_operations": ("iter_operations", None, "OperationSummary"),
    "list_files": ("iter_files", "files", "File"),
}


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


def split_union(annotation: str) -> list[str]:
    """Split a python annotation union at the top level only."""
    parts: list[str] = []
    depth = 0
    current = ""
    for char in annotation:
        if char in "[(":
            depth += 1
        elif char in "])":
            depth -= 1
        if char == "|" and depth == 0:
            parts.append(current.strip())
            current = ""
            continue
        current += char
    parts.append(current.strip())
    return [part for part in parts if part]


TS_ATOMS = {
    "str": "string",
    "int": "number",
    "float": "number",
    "bool": "boolean",
    "Any": "unknown",
    "None": "null",
    "EllipsisType": "undefined",
    "datetime": "Date",
    "bytes": "Uint8Array",
    "IO[bytes]": "Blob | ReadableStream<Uint8Array>",
    "OperationStatus": "OperationStatus",
    "OperationEventType": "OperationEventType",
    "EventData": "EventData",
    "dict[str, Any]": "Record<string, unknown>",
}


def ts_type(annotation: str) -> str:
    """Translate a python annotation into a TypeScript type."""
    parts = split_union(annotation)
    if len(parts) > 1:
        rendered = [ts_type(part) for part in parts]
        seen: list[str] = []
        for part in rendered:
            if part not in seen:
                seen.append(part)
        return " | ".join(seen)
    atom = parts[0]
    if atom in TS_ATOMS:
        return TS_ATOMS[atom]
    if atom.startswith("list["):
        return f"{ts_type(atom[5:-1])}[]"
    if atom.startswith("dict[str, "):
        return f"Record<string, {ts_type(atom[10:-1])}>"
    if atom.startswith("Literal["):
        values = atom[8:-1].replace("'", '"')
        return values.replace(", ", " | ")
    return atom  # a model class name


def parse_expr(annotation: str, expr: str) -> str | None:
    """A JS expression decoding a wire value, or None for identity."""
    if annotation == "OperationInstanceMetadata | ImageImportMetadata":
        return (
            f'data["kind"] === "instance"'
            f" ? OperationInstanceMetadata.fromWire({expr})"
            f" : ImageImportMetadata.fromWire({expr})"
        )
    if annotation == "EventData | dict[str, Any]":
        return f'parseEventData(data["type"], {expr})'
    parts = split_union(annotation)
    if len(parts) > 1:
        return None  # untyped unions (str | int, ...) pass through
    atom = parts[0]
    if atom == "datetime":
        return f"parseDatetime({expr})"
    if atom.startswith("list["):
        inner = parse_expr(atom[5:-1], "item")
        if inner is None:
            return None
        return f"{expr}.map((item) => {inner})"
    if atom.startswith("dict[str, "):
        inner = parse_expr(atom[10:-1], "value")
        if inner is None:
            return None
        return (
            f"Object.fromEntries(Object.entries({expr})"
            f".map(([key, value]) => [key, {inner}]))"
        )
    if atom in TS_ATOMS or atom.startswith("Literal["):
        return None
    return f"{atom}.fromWire({expr})"  # a model class name


def dump_expr(annotation: str, expr: str) -> str | None:
    """A JS expression encoding a value for the wire, or None."""
    if annotation == "OperationInstanceMetadata | ImageImportMetadata":
        return f"{expr}.toWire()"
    if annotation == "EventData | dict[str, Any]":
        return f'(typeof {expr}.toWire === "function" ? {expr}.toWire() : {expr})'
    parts = split_union(annotation)
    if len(parts) > 1:
        return None
    atom = parts[0]
    if atom == "datetime":
        return f"{expr}.toISOString()"
    if atom.startswith("list["):
        inner = dump_expr(atom[5:-1], "item")
        if inner is None:
            return None
        return f"{expr}.map((item) => {inner})"
    if atom.startswith("dict[str, "):
        inner = dump_expr(atom[10:-1], "value")
        if inner is None:
            return None
        return (
            f"Object.fromEntries(Object.entries({expr})"
            f".map(([key, value]) => [key, {inner}]))"
        )
    if atom in TS_ATOMS or atom.startswith("Literal["):
        return None
    return f"{expr}.toWire()"


def field_parse(fld: FieldDef) -> str:
    src = f'data["{fld.json_name}"]'
    parse = parse_expr(fld.type.annotation, src)
    if parse is None:
        return src
    if fld.required and not fld.nullable:
        return parse
    # null and undefined (unset) both pass through untouched
    return f"{src} == null ? {src} : {parse}"


def field_dump(fld: FieldDef) -> list[str]:
    src = f"this.{fld.py_name}"
    dump = dump_expr(fld.type.annotation, src)
    value = src if dump is None else f"{src} === null ? null : {dump}"
    return [
        f"if ({src} !== undefined) {{",
        f'  data["{fld.json_name}"] = {value};',
        "}",
    ]


def field_ts(fld: FieldDef) -> str:
    base = ts_type(fld.type.annotation)
    if fld.nullable and not base.endswith("| null"):
        base = f"{base} | null"
    optional = "?" if not fld.required else ""
    return f"{fld.py_name}{optional}: {base};"


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


def render_class_js(cls: ClassDef) -> str:
    lines: list[str] = []
    if cls.description:
        lines.append(f"/** {cls.description.splitlines()[0]} */")
    lines.append(f"export class {cls.name} {{")
    lines.append("  constructor(fields = {}) {")
    lines.extend(
        f"    this.{fld.py_name} = fields.{fld.py_name};" for fld in cls.ordered_fields
    )
    if cls.name == "FileSpec":
        lines.append(FILESPEC_MODE_JS.rstrip())
    lines.append("  }")
    lines.append("")
    lines.append("  static fromWire(data) {")
    lines.append(f"    return new {cls.name}({{")
    lines.extend(
        f"      {fld.py_name}: {field_parse(fld)}," for fld in cls.ordered_fields
    )
    lines.append("    });")
    lines.append("  }")
    lines.append("")
    lines.append("  toWire() {")
    lines.append("    const data = {};")
    for fld in cls.ordered_fields:
        lines.extend(f"    {line}" for line in field_dump(fld))
    lines.append("    return data;")
    lines.append("  }")
    if cls.name in ("StreamRepr", "EventDataStream"):
        lines.append(STREAM_VALUE_METHODS_JS.rstrip())
    lines.append("}")
    return "\n".join(lines)


def render_class_dts(cls: ClassDef) -> str:
    fields = [f"  {field_ts(fld)}" for fld in cls.ordered_fields]
    ctor_fields = " ".join(
        f"{fld.py_name}?: {ts_type(fld.type.annotation)}"
        + (" | null;" if fld.nullable or not fld.required else ";")
        for fld in cls.ordered_fields
    )
    lines = [f"export declare class {cls.name} {{"]
    lines.extend(fields)
    lines.append(f"  constructor(fields?: {{ {ctor_fields} }});")
    lines.append(f"  static fromWire(data: Record<string, unknown>): {cls.name};")
    lines.append("  toWire(): Record<string, unknown>;")
    if cls.name in ("StreamRepr", "EventDataStream"):
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
        f"  {event}: {name}," for event, name in EVENT_DATA_CLASSES.items()
    )
    parts = [
        HEADER,
        'import { base64ToBytes, bytesToBase64, bytesToText, parseDatetime, textToBytes } from "./runtime.js";',
        f"export const OPERATION_EVENT_TYPES = Object.freeze([{event_types}]);",
        "/** Operation lifecycle state. */\n"
        f"export const OperationStatus = Object.freeze({{\n{statuses},\n}});",
        f"export const TERMINAL_STATUSES = new Set([{terminal}]);",
        f"export const ACTIVE_STATUSES = new Set([{active}]);",
        "/** True for statuses that will never change again. */\n"
        "export function isTerminalStatus(status) {\n"
        "  return TERMINAL_STATUSES.has(status);\n"
        "}",
    ]
    parts.extend(render_class_js(cls) for cls in ir.classes)
    parts.append(MODELS_TAIL_JS.format(parsers=parsers).strip())
    return "\n\n".join(parts) + "\n"


def render_models_dts(ir: SpecIR) -> str:
    statuses = " | ".join(f'"{value}"' for value in ir.status_values)
    event_types = " | ".join(f'"{value}"' for value in ir.event_type_values)
    status_consts = " ".join(f'{value}: "{value}";' for value in ir.status_values)
    event_classes = sorted(set(EVENT_DATA_CLASSES.values()))
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
    parts.extend(render_class_dts(cls) for cls in ir.classes)
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


def required_args(op: OpDef) -> list[str]:
    return [arg.py_name for arg in op.args if arg.default is None]


def optional_args(op: OpDef) -> list[tuple[str, str | None]]:
    """(name, js_default) pairs; None means plain destructure (unset)."""
    defaults = {"None": "null", "False": "false", "...": None}
    return [
        (arg.py_name, defaults.get(arg.default or "", "null"))
        for arg in op.args
        if arg.default is not None
    ]


def js_params(op: OpDef, destructure: bool) -> str:
    """The parameter list of a builder/method.

    Required args are positional (camelCase locals); optional args
    arrive in a trailing options object - destructured in builders,
    passed through whole in client methods.
    """
    parts = [js_local(camel(name)) for name in required_args(op)]
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


def js_reference(op: OpDef, py_name: str) -> str:
    """How a builder body refers to the argument *py_name*."""
    if py_name in required_args(op):
        return js_local(camel(py_name))
    return js_local(py_name)


def render_build_fn(op: OpDef) -> str:
    name = camel(f"build_{op.name}")
    body: list[str] = []
    query = [p for p in op.params if p.where == "query"]
    headers = [p for p in op.params if p.where == "header"]
    if query:
        body.append("const query = {};")
        for param in query:
            ref = js_reference(op, param.py_name)
            target = f'query["{param.json_name}"]'
            if param.style == "flag":
                body.append(f"if ({ref}) {{")
                body.append(f'  {target} = "1";')
                body.append("}")
                continue
            if param.style == "time":
                value = f"formatTimeParam({ref})"
            elif param.style in ("int", "status"):
                value = f"String({ref})"
            else:
                value = ref
            if param.required:
                body.append(f"{target} = {value};")
            else:
                body.append(f"if ({ref} != null) {{")
                body.append(f"  {target} = {value};")
                body.append("}")
    if headers:
        body.append("const headers = {};")
        for param in headers:
            ref = js_reference(op, param.py_name)
            body.append(f"if ({ref} != null) {{")
            body.append(f'  headers["{param.json_name}"] = String({ref});')
            body.append("}")
    if op.body_kind == "json_model":
        ctor = ", ".join(
            name
            if js_reference(op, name) == name
            else f"{name}: {js_reference(op, name)}"
            for name in [arg.py_name for arg in op.args]
            if name != "content"
        )
        body.append(f"const payload = new {op.body_model}({{ {ctor} }}).toWire();")
    elif op.body_kind == "json_inline":
        body.append("const payload = {};")
        for fld in op.body_fields:
            ref = js_reference(op, fld.py_name)
            if fld.required:
                body.append(f'payload["{fld.json_name}"] = {ref};')
            else:
                body.append(f"if ({ref} !== undefined) {{")
                body.append(f'  payload["{fld.json_name}"] = {ref};')
                body.append("}")
    path = op.path
    for param in op.params:
        if param.where == "path":
            path = path.replace(
                "{" + param.json_name + "}",
                "${quotePath(" + js_reference(op, param.py_name) + ")}",
            )
    spec_fields = [
        f'method: "{op.http_method}"',
        f"path: `{path}`",
        f"idempotent: {'true' if op.http_method in ('GET', 'HEAD', 'PUT', 'DELETE') else 'false'}",
    ]
    if query:
        spec_fields.append("query")
    if headers:
        spec_fields.append("headers")
    if op.body_kind in ("json_model", "json_inline"):
        spec_fields.append("body: JSON.stringify(payload)")
        spec_fields.append('contentType: "application/json"')
    elif op.body_kind == "binary":
        spec_fields.append("body: content")
        spec_fields.append('contentType: "application/octet-stream"')
    if op.kind == "sse":
        spec_fields.append('accept: "text/event-stream"')
    if op.response_kind == "location":
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


def render_parse_fn(op: OpDef) -> str | None:
    if op.kind in ("sse", "stream"):
        return None
    name = camel(f"parse_{op.name}")
    success = "response.status >= 200 && response.status < 300"
    body: list[str] = []
    kind = op.response_kind
    if kind == "model":
        body += [
            f"if ({success}) {{",
            f"  return {op.response_model}.fromWire(jsonObject(response));",
            "}",
        ]
    elif kind == "list_model":
        body += [
            f"if ({success}) {{",
            f"  return jsonArray(response).map((item) => {op.response_model}.fromWire(item));",
            "}",
        ]
    elif kind in ("str_field", "int_field"):
        cast = "String" if kind == "str_field" else "Number"
        body += [
            f"if ({success}) {{",
            f'  return {cast}(jsonObject(response)["{op.response_model}"]);',
            "}",
        ]
    elif kind == "location":
        body += [
            "// Node exposes the 302 Location; browsers only expose the",
            "// followed response, whose final URL ends with the UUID",
            f'if (response.status === {op.success_status} && response.headers["location"]) {{',
            '  const location = response.headers["location"];',
            '  return location.replace(/\\/+$/, "").split("/").pop();',
            "}",
            f"if (({success}) && response.url) {{",
            "  const path = new URL(response.url).pathname;",
            '  return path.replace(/\\/+$/, "").split("/").pop();',
            "}",
        ]
    elif kind == "bool":
        body += [
            f"if ({success}) {{",
            "  return true;",
            "}",
            "if (response.status === 404) {",
            "  return false;",
            "}",
        ]
    elif kind == "bytes":
        body += [f"if ({success}) {{", "  return response.body;", "}"]
    else:  # none
        body += [f"if ({success}) {{", "  return null;", "}"]
    body.append("throw errorForResponse(response);")
    lines = [
        f"/** Parse the response of `{op.http_method} {op.path}`. */",
        f"export function {name}(response) {{",
        *[f"  {line}" for line in body],
        "}",
    ]
    return "\n".join(lines)


def render_operations_js(ir: SpecIR) -> str:
    parts = [HEADER]
    models = sorted(
        {op.body_model for op in ir.operations if op.body_model}
        | {
            op.response_model
            for op in ir.operations
            if op.response_model and op.response_kind in ("model", "list_model")
        }
    )
    imports = [
        "errorForResponse",
        "formatTimeParam",
        "jsonArray",
        "jsonObject",
        "quotePath",
    ]
    parts.append(f'import {{ {", ".join(imports)} }} from "./runtime.js";')
    if models:
        parts.append(f'import {{ {", ".join(models)} }} from "./models.js";')
    for op in ir.operations:
        parts.append(render_build_fn(op))
        parse = render_parse_fn(op)
        if parse:
            parts.append(parse)
    return "\n\n".join(parts) + "\n"


def option_ts_entries(op: OpDef) -> str:
    entries = []
    for arg in op.args:
        if arg.default is None:
            continue
        base = ts_type(arg.annotation)
        entries.append(f"{arg.py_name}?: {base};")
    return " ".join(entries)


def ts_params(op: OpDef) -> str:
    parts = [
        f"{camel(arg.py_name)}: {ts_type(arg.annotation)}"
        for arg in op.args
        if arg.default is None
    ]
    entries = option_ts_entries(op)
    if entries:
        parts.append(f"options?: {{ {entries} }}")
    return ", ".join(parts)


def model_type_names(ir: SpecIR) -> list[str]:
    """Every name models.d.ts exports that other declarations may use."""
    return [
        *ir.class_names,
        "OperationStatus",
        "OperationEventType",
        "EventData",
    ]


def render_operations_dts(ir: SpecIR) -> str:
    decls: list[str] = []
    for op in ir.operations:
        build = camel(f"build_{op.name}")
        decls.append(f"export declare function {build}({ts_params(op)}): RequestSpec;")
        if op.kind in ("sse", "stream"):
            continue
        parse = camel(f"parse_{op.name}")
        decls.append(
            f"export declare function {parse}(response: ResponseData): {ts_type(op.return_annotation)};"
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
  ContreeAPIError,
  ContreeError,
  NotFoundError,
  SSEStreamError,
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
  errorForResponse,
  monotonic,
  retryAfterDelay,
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
    this.token = token;
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
    const headers = {{ Authorization: `Bearer ${{this.token}}` }};
    if (this.project !== null) {{
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

  /** Execute the request and return the buffered response. */
  async request(spec) {{
    const controller = new AbortController();
    // an explicit timer, not AbortSignal.timeout(): Node unrefs that
    // timer, so it can never fire when nothing else keeps the event
    // loop alive (custom fetch transports, for one)
    const timer =
      this.timeout !== null
        ? setTimeout(
            () =>
              controller.abort(
                new DOMException("request timed out", "TimeoutError"),
              ),
            this.timeout * 1000,
          )
        : null;
    try {{
      const response = await this._fetch(
        this.buildUrl(spec),
        this._fetchOptions(spec, controller.signal),
      );
      const body = new Uint8Array(await response.arrayBuffer());
      const headers = {{}};
      response.headers.forEach((value, key) => {{
        headers[key.toLowerCase()] = value;
      }});
      return {{ status: response.status, headers, body, url: response.url }};
    }} finally {{
      if (timer !== null) {{
        clearTimeout(timer);
      }}
    }}
  }}

  /** fetch reports network failures as TypeError; timeouts abort. */
  _transportRetryable(error) {{
    return error instanceof TypeError;
  }}

  _transportNonretryable(error) {{
    return (
      error instanceof DOMException &&
      (error.name === "AbortError" || error.name === "TimeoutError")
    );
  }}

  /** One transparent reconnect for a replayable idempotent request:
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
        !this._transportRetryable(error) ||
        this._transportNonretryable(error)
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
    if (!spec.idempotent && !policy.retryUnsafe) {{
      // a lost response after a non-idempotent request (POST) could
      // mean a second execution server-side
      return await this.request(spec);
    }}
    if (
      typeof ReadableStream !== "undefined" &&
      spec.body instanceof ReadableStream
    ) {{
      // a stream cannot be replayed: single attempt
      return await this.request(spec);
    }}
    const delays = retryDelays(policy.delays);
    let attempts = 0;
    for (;;) {{
      attempts += 1;
      const exhausted =
        policy.maxAttempts !== null && attempts >= policy.maxAttempts;
      let response;
      try {{
        response = await this.request(spec);
      }} catch (error) {{
        if (
          !this._transportRetryable(error) ||
          this._transportNonretryable(error) ||
          exhausted
        ) {{
          throw error;
        }}
        await sleep(delays.next().value);
        continue;
      }}
      if (!policy.retryableStatus(response.status) || exhausted) {{
        return response;
      }}
      const retryAfter = retryAfterDelay(response);
      await sleep(retryAfter !== null ? retryAfter : delays.next().value);
    }}
  }}

  /** Execute the request and yield response body chunks. */
  async *stream(spec) {{
    const controller = new AbortController();
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
        Math.max(0, seconds) * 1000,
      );
    }};
    const sse = spec.accept === "text/event-stream";
    const deadline = spec.deadline ?? null; // monotonic seconds
    // the budget for the next transport wait: downloads use the
    // client timeout (idle cap, re-armed per chunk); SSE is unbounded
    // unless the caller set an absolute deadline - keepalive frames
    // re-enter abortIn with a shrinking remainder, so the bound stays
    // absolute no matter how often the server keeps the stream warm
    const nextBudget = () => {{
      if (!sse) {{
        return this.timeout;
      }}
      return deadline === null ? null : deadline - monotonic();
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
    const policy = replayable ? this.retry : null;
    const maxAttempts =
      policy !== null ? policy.maxAttempts : replayable ? 2 : 1;
    const delays = policy === null ? null : retryDelays(policy.delays);
    let attempts = 0;
    let response;
    for (;;) {{
      attempts += 1;
      abortIn(this.timeout); // the connect phase always has a bound
      try {{
        response = await this._fetch(
          this.buildUrl(spec),
          this._fetchOptions(spec, controller.signal),
        );
        break;
      }} catch (error) {{
        disarm();
        if (
          !this._transportRetryable(error) ||
          this._transportNonretryable(error) ||
          (maxAttempts !== null && attempts >= maxAttempts)
        ) {{
          throw error;
        }}
        await sleep(delays === null ? 0 : delays.next().value);
      }}
    }}
    if (!(response.status >= 200 && response.status < 300)) {{
      // the error body is read under the same timer: a 500 with an
      // endless body must not hang the caller
      let body;
      try {{
        body = new Uint8Array(await response.arrayBuffer());
      }} finally {{
        disarm();
      }}
      const headers = {{}};
      response.headers.forEach((value, key) => {{
        headers[key.toLowerCase()] = value;
      }});
      throw errorForResponse({{ status: response.status, headers, body }});
    }}
    disarm();
    const reader = response.body.getReader();
    try {{
      for (;;) {{
        abortIn(nextBudget());
        const {{ done, value }} = await reader.read();
        // the timer covers only the transport wait, never the
        // consumer's processing of the yielded chunk
        disarm();
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
  async operationTerminal(operationId) {{
    let status;
    try {{
      status = (await this.getOperationStatus(operationId)).status;
    }} catch (error) {{
      if (error instanceof ContreeError || this._transportRetryable(error)) {{
        return false;
      }}
      throw error;
    }}
    return status !== undefined && isTerminalStatus(status);
  }}

  /** Wait until the operation finishes, driven by its event stream:
   * follows the SSE log until `completion`, then fetches and returns
   * the terminal OperationResponse. */
  async waitOperation(operationId, {{ timeout = null }} = {{}}) {{
    // eslint-disable-next-line no-unused-vars
    for await (const event of this.followOperationEvents(operationId, {{ timeout }})) {{
      // draining the stream is the wait
    }}
    return await this.getOperationStatus(operationId);
  }}

  /** Stream operation events with transparent reconnection: network
   * drops, in-band SSE error frames and retryable API statuses
   * (410/425/5xx) reconnect from the last received event id; other
   * API errors propagate. Ends after the `completion` event. */
  async *followOperationEvents(
    operationId,
    {{ last_event_id = null, spid = null, since = null, timeout = null }} = {{}},
  ) {{
    let lastId = last_event_id;
    const delays = retryDelays();
    const deadline = timeout === null ? null : monotonic() + timeout;
    const checkDeadline = () => {{
      if (deadline !== null && monotonic() >= deadline) {{
        throw new ContreeAPIError(
          0,
          `operation ${{operationId}} events did not complete within ${{timeout}}s`,
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
        if (error instanceof SSEStreamError) {{
          if (error.lastEventId !== null) {{
            lastId = error.lastEventId;
          }}
        }} else if (error instanceof ContreeAPIError) {{
          const retryable =
            error.status === 410 ||
            error.status === 425 ||
            (error.status >= 500 && error.status < 600);
          if (!retryable) {{
            throw error;
          }}
          if (await this.operationTerminal(operationId)) {{
            return;
          }}
          let delay =
            error.retryAfter !== null ? error.retryAfter : delays.next().value;
          if (deadline !== null) {{
            // a Retry-After must not sleep past the caller's deadline
            delay = Math.min(delay, Math.max(0, deadline - monotonic()));
          }}
          await sleep(delay);
          continue;
        }} else if (
          !this._transportRetryable(error) ||
          this._transportNonretryable(error)
        ) {{
          throw error;
        }}
      }}
      // the stream ended or broke without a completion frame: the
      // retry must not outlive the operation itself
      if (await this.operationTerminal(operationId)) {{
        return;
      }}
      if (lastId === eventsBefore) {{
        await sleep(TIGHT_LOOP_FLOOR);
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


def method_call_args(op: OpDef) -> str:
    parts = [camel(name) for name in required_args(op)]
    if optional_args(op):
        parts.append("options")
    return ", ".join(parts)


def render_client_method(op: OpDef) -> list[str]:
    name = camel(op.name)
    build = camel(f"build_{op.name}")
    parse = camel(f"parse_{op.name}")
    params = js_params(op, destructure=False)
    call_args = method_call_args(op)
    doc = f"  /** {op.summary or op.name} ({op.http_method} {op.path}) */"
    blocks: list[str] = []
    if op.kind in ("call", "bytes"):
        blocks.append(
            f"{doc}\n"
            f"  async {name}({params}) {{\n"
            f"    const spec = operations.{build}({call_args});\n"
            f"    return operations.{parse}(await this.call(spec));\n"
            f"  }}"
        )
        if op.stream_variant:
            stream_name = camel(f"{op.name}_stream")
            blocks.append(
                f"  /** Streaming variant of {name}(). */\n"
                f"  async *{stream_name}({params}) {{\n"
                f"    const spec = operations.{build}({call_args});\n"
                f"    yield* this.stream(spec);\n"
                f"  }}"
            )
    elif op.kind == "stream":
        blocks.append(
            f"{doc}\n"
            f"  async *{name}({params}) {{\n"
            f"    const spec = operations.{build}({call_args});\n"
            f"    yield* this.stream(spec);\n"
            f"  }}"
        )
    elif op.kind == "sse":
        blocks.append(
            f"{doc}\n"
            f"  async *{name}({params}) {{\n"
            f"    const spec = operations.{build}({call_args});\n"
            f"    if (options.deadline != null) {{{{\n"
            f"      spec.deadline = options.deadline;\n"
            f"    }}}}\n"
            f"    const parser = new SSEParser();\n"
            f"    let lastSeen = options.last_event_id ?? null;\n"
            f"    for await (const chunk of this.stream(spec)) {{\n"
            f"      for (const frame of parser.feed(chunk)) {{\n"
            f"        const payload = decodeFramePayload(frame, lastSeen);\n"
            f"        // id-only frames advance the resume cursor even\n"
            f"        // though they carry no payload\n"
            f"        if (frame.id !== null) {{\n"
            f"          lastSeen = frame.id;\n"
            f"        }}\n"
            f"        if (payload === null) {{\n"
            f"          continue;\n"
            f"        }}\n"
            f"        yield OperationEvent.fromWire(payload);\n"
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
        iter_spec = ITER_LIST_OPERATIONS.get(op.name)
        if iter_spec is not None:
            iter_name, attr, _item = iter_spec
            page_expr = "response" if attr is None else f"response.{attr}"
            methods.append(
                ITER_METHOD_JS.strip("\n").format(
                    name=camel(iter_name),
                    list_method=camel(op.name),
                    page_expr=page_expr,
                    page_max=op.page_limit_max or 1000,
                )
            )
    body = "\n\n".join(methods)
    return f"{parts[0]}\n{parts[1]}\n\n{body}\n}}\n"


def client_method_dts(op: OpDef) -> list[str]:
    name = camel(op.name)
    params = ts_params(op)
    lines: list[str] = []
    if op.kind in ("call", "bytes"):
        lines.append(f"  {name}({params}): Promise<{ts_type(op.return_annotation)}>;")
        if op.stream_variant:
            lines.append(
                f"  {camel(op.name + '_stream')}({params}): AsyncGenerator<Uint8Array>;"
            )
    elif op.kind == "stream":
        lines.append(f"  {name}({params}): AsyncGenerator<Uint8Array>;")
    elif op.kind == "sse":
        lines.append(f"  {name}({params}): AsyncGenerator<OperationEvent>;")
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
  token: string;
  baseUrl: string;
  project: string | null;
  timeout: number | null;
  retry: RetryPolicy | null;
  identity: string | null;
  constructor(token: string, options?: ContreeClientOptions);
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
  iterImages(options?: {
    tagged?: boolean;
    tag?: string | null;
    uuid?: string | null;
    since?: string | number | Date | null;
    until?: string | number | Date | null;
    page_size?: number;
    limit?: number | null;
  }): AsyncGenerator<Image>;
  iterOperations(options?: {
    status?: string | null;
    kind?: string | null;
    since?: string | number | Date | null;
    until?: string | number | Date | null;
    page_size?: number;
    limit?: number | null;
  }): AsyncGenerator<OperationSummary>;
  iterFiles(options?: {
    since?: string | number | Date | null;
    until?: string | number | Date | null;
    page_size?: number;
    limit?: number | null;
  }): AsyncGenerator<File>;
"""


def render_client_dts(ir: SpecIR) -> str:
    lines = [CLIENT_DTS_HEADER.strip("\n")]
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


def op_signature(op: OpDef) -> str:
    parts = [camel(name) for name in required_args(op)]
    if optional_args(op):
        parts.append("options?")
    return ", ".join(parts)


def op_returns(op: OpDef) -> str:
    if op.kind == "sse":
        return "AsyncGenerator<OperationEvent>"
    if op.kind == "stream":
        return "AsyncGenerator<Uint8Array>"
    return f"Promise<{ts_type(op.return_annotation)}>"


def render_op_reference(op: OpDef) -> str:
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
    for arg in op.args:
        if arg.default is None:
            name = f"param {camel(arg.py_name)}"
        else:
            name = f"param options.{arg.py_name}"
        doc = " ".join(sanitize_doc(arg.doc, escape=False).split())
        text = f"``{ts_type(arg.annotation)}``" + (f" — {doc}" if doc else "")
        body.extend(rst_field(name, text))
    body.extend(rst_field("returns", f"``{op_returns(op)}``"))
    lines.extend(indented(body))
    if op.stream_variant:
        stream_sig = ", ".join(camel(name) for name in required_args(op))
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


def render_model_reference(cls: ClassDef) -> str:
    lines = [f".. js:class:: {cls.name}(fields?)", ""]
    body: list[str] = []
    if cls.description:
        body.extend(doc_block_lines(cls.description, escape=False))
        body.append("")
    for fld in cls.ordered_fields:
        base = ts_type(fld.type.annotation)
        if fld.nullable and not base.endswith("| null"):
            base = f"{base} | null"
        body.append(f".. js:attribute:: {cls.name}.{fld.py_name}")
        body.append("")
        marker = "required" if fld.required else "optional"
        doc = " ".join(sanitize_doc(fld.doc, escape=False).split())
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
    if cls.name in ("StreamRepr", "EventDataStream"):
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
   and transparent reconnection: network drops, in-band ``sse_error``
   frames and retryable statuses (410/425/5xx) resume from the last
   received event id; other API errors propagate. Ends after the
   ``completion`` frame.

   :param operationId: ``string``
   :param options.last_event_id: ``number | null``
   :param options.spid: ``number | null``
   :param options.since: ``number | null``
   :param options.timeout: ``number | null`` — overall deadline in
      seconds.
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
        "Generated from the OpenAPI specification"
        f" (SHA-256 ``{ir.spec_sha256[:12]}…``). Methods are camelCase;"
        " model fields and option keys keep their snake_case wire"
        " spelling. Required arguments are positional, optional ones"
        " ride in a trailing ``options`` object.",
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
    parts.extend(render_model_reference(cls) for cls in ir.classes)
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
  parseDatetime,
  parseRetryAfter,
  sha256,
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
  parseDatetime,
  parseRetryAfter,
  sha256,
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
