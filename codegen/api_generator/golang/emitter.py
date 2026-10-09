"""Render the model kernel for the generated Go client."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from collections.abc import Iterable
from pathlib import Path

from ..emitter import Emitter
from ..ir import (
    FieldDef,
    ModelDef,
    ModelTrait,
    SpecIR,
    TypeKind,
    TypeRef,
)
from .naming import (
    HEADER,
    _go_string,
    go_name,
    go_type,
    is_string_integer_union,
    model_field_names,
    validate_package_name,
)
from .operations import render_operations
from .pagination import render_pagination
from .testing import render_testing

GENERATED_FILES = (
    "models.gen.go",
    "operations.gen.go",
    "spec_info.gen.go",
    "testing.gen.go",
)
DEFAULT_PACKAGE_NAME = "contree"


def _walk_type(type_ref: TypeRef) -> Iterable[TypeRef]:
    yield type_ref
    for argument in type_ref.arguments:
        yield from _walk_type(argument)


def _all_type_refs(ir: SpecIR) -> Iterable[TypeRef]:
    for model in ir.models:
        for field in model.fields:
            yield from _walk_type(field.type)
            if field.discriminator is None:
                continue
            yield from _walk_type(field.discriminator.fallback)
            for _, case_type in field.discriminator.cases:
                yield from _walk_type(case_type)


def _discriminator_base(field: FieldDef) -> str:
    discriminator = field.discriminator
    if discriminator is not None and discriminator.name == "EventData":
        return "EventData"
    return "any"


def _field_base_type(field: FieldDef) -> str:
    if field.discriminator is not None:
        return _discriminator_base(field)
    return go_type(field.type)


def _field_type(field: FieldDef) -> str:
    base = _field_base_type(field)
    if not field.required:
        return f"Optional[{base}]"
    if field.nullable and field.discriminator is None:
        return "*" + base
    return base


def _field_requires_non_nil(field: FieldDef) -> bool:
    if not field.required or field.nullable:
        return False
    if field.discriminator is not None:
        return True
    if field.type.kind in {
        TypeKind.ANY,
        TypeKind.BYTES,
        TypeKind.BINARY_STREAM,
        TypeKind.LIST,
        TypeKind.MAP,
        TypeKind.SEQUENCE,
    }:
        return True
    if field.type.kind is TypeKind.ALIAS and field.type.name == "EventData":
        return True
    return field.type.kind is TypeKind.UNION and not is_string_integer_union(field.type)


def _struct_tag(field: FieldDef) -> str:
    if "`" in field.wire_name:
        return ""
    wire = _go_string(field.wire_name)[1:-1]
    return f' `json:"{wire}"`'


OPTIONAL_SUPPORT = r"""
// Optional preserves whether a JSON property was absent, null, or a value.
type Optional[T any] struct {
	value T
	set   bool
	null  bool
}

// Some constructs an Optional that contains value.
func Some[T any](value T) Optional[T] {
	if isNilJSONValue(value) {
		return Null[T]()
	}
	return Optional[T]{value: value, set: true}
}

// Null constructs an Optional that contains an explicit JSON null.
func Null[T any]() Optional[T] {
	return Optional[T]{set: true, null: true}
}

// IsSet reports whether the property was present.
func (o Optional[T]) IsSet() bool { return o.set }

// IsNull reports whether the property was present as JSON null.
func (o Optional[T]) IsNull() bool { return o.set && o.null }

// Value returns the value and reports whether it is present and non-null.
func (o Optional[T]) Value() (T, bool) { return o.value, o.set && !o.null }

// Set replaces the state with a value.
func (o *Optional[T]) Set(value T) {
	if isNilJSONValue(value) {
		o.SetNull()
		return
	}
	o.value = value
	o.set = true
	o.null = false
}

// SetNull replaces the state with an explicit JSON null.
func (o *Optional[T]) SetNull() {
	var zero T
	o.value = zero
	o.set = true
	o.null = true
}

// Unset makes the property absent.
func (o *Optional[T]) Unset() {
	var zero T
	o.value = zero
	o.set = false
	o.null = false
}

// MarshalJSON implements json.Marshaler. A standalone unset Optional marshals
// as null, while an enclosing generated model omits its unset fields.
func (o Optional[T]) MarshalJSON() ([]byte, error) {
	if !o.set || o.null {
		return []byte("null"), nil
	}
	return json.Marshal(o.value)
}

// UnmarshalJSON implements json.Unmarshaler.
func (o *Optional[T]) UnmarshalJSON(data []byte) error {
	if o == nil {
		return fmt.Errorf("unmarshal Optional into nil receiver")
	}
	if isJSONNull(data) {
		o.SetNull()
		return nil
	}
	var value T
	if err := json.Unmarshal(data, &value); err != nil {
		return err
	}
	o.Set(value)
	return nil
}
"""


UNION_SUPPORT = r"""
// StringOrInt64 represents the string-or-integer union used by file modes.
type StringOrInt64 struct {
	value any
}

// StringOrInt64FromString constructs the string variant.
func StringOrInt64FromString(value string) StringOrInt64 {
	return StringOrInt64{value: value}
}

// StringOrInt64FromInt64 constructs the integer variant.
func StringOrInt64FromInt64(value int64) StringOrInt64 {
	return StringOrInt64{value: value}
}

// StringValue returns the string variant.
func (u StringOrInt64) StringValue() (string, bool) {
	value, ok := u.value.(string)
	return value, ok
}

// Int64Value returns the integer variant.
func (u StringOrInt64) Int64Value() (int64, bool) {
	value, ok := u.value.(int64)
	return value, ok
}

// MarshalJSON implements json.Marshaler.
func (u StringOrInt64) MarshalJSON() ([]byte, error) {
	switch value := u.value.(type) {
	case string:
		return json.Marshal(value)
	case int64:
		return json.Marshal(value)
	default:
		return nil, fmt.Errorf("StringOrInt64 has no value")
	}
}

// UnmarshalJSON implements json.Unmarshaler.
func (u *StringOrInt64) UnmarshalJSON(data []byte) error {
	if u == nil {
		return fmt.Errorf("unmarshal StringOrInt64 into nil receiver")
	}
	trimmed := bytes.TrimSpace(data)
	if len(trimmed) == 0 || isJSONNull(trimmed) {
		return fmt.Errorf("StringOrInt64 must be a string or integer")
	}
	if trimmed[0] == '"' {
		var value string
		if err := json.Unmarshal(trimmed, &value); err != nil {
			return err
		}
		u.value = value
		return nil
	}
	decoder := json.NewDecoder(bytes.NewReader(trimmed))
	decoder.UseNumber()
	var value any
	if err := decoder.Decode(&value); err != nil {
		return err
	}
	number, ok := value.(json.Number)
	if !ok {
		return fmt.Errorf("StringOrInt64 must be a string or integer")
	}
	integer, err := number.Int64()
	if err != nil {
		return fmt.Errorf("StringOrInt64 integer: %w", err)
	}
	u.value = integer
	return nil
}
"""


MODEL_HELPERS = r"""
func isJSONNull(data []byte) bool {
	return bytes.Equal(bytes.TrimSpace(data), []byte("null"))
}

func putJSONField(object map[string]json.RawMessage, name string, value any) error {
	encoded, err := json.Marshal(value)
	if err != nil {
		return err
	}
	object[name] = encoded
	return nil
}

func readDiscriminator(object map[string]json.RawMessage, name string) (string, error) {
	raw, ok := object[name]
	if !ok || isJSONNull(raw) {
		return "", nil
	}
	var value string
	if err := json.Unmarshal(raw, &value); err != nil {
		return "", err
	}
	return value, nil
}

func isNilJSONValue(value any) bool {
	if value == nil {
		return true
	}
	reflected := reflect.ValueOf(value)
	switch reflected.Kind() {
	case reflect.Chan, reflect.Func, reflect.Interface, reflect.Map,
		reflect.Pointer, reflect.Slice:
		return reflected.IsNil()
	default:
		return false
	}
}
"""


def _render_imports(ir: SpecIR) -> str:
    imports = {"bytes", "encoding/json", "fmt", "reflect"}
    type_kinds = {item.kind for item in _all_type_refs(ir)}
    if TypeKind.BINARY_STREAM in type_kinds:
        imports.add("io")
    if TypeKind.DATETIME in type_kinds:
        imports.add("time")
    if any(ModelTrait.STREAM_VALUE in model.traits for model in ir.models):
        imports.update(("encoding/base64", "strings"))
    lines = ["import ("]
    lines.extend(f'\t"{name}"' for name in sorted(imports))
    lines.append(")")
    return "\n".join(lines)


def _unique_constants(prefix: str, values: Iterable[str]) -> list[tuple[str, str]]:
    result: list[tuple[str, str]] = []
    used: set[str] = set()
    for value in values:
        base = prefix + go_name(value)
        name = base
        suffix = 2
        while name in used:
            name = f"{base}{suffix}"
            suffix += 1
        used.add(name)
        result.append((name, value))
    return result


def _render_status(ir: SpecIR) -> str:
    constants = _unique_constants("OperationStatus", ir.status_values)
    lines = [
        "// OperationStatus is an operation lifecycle state.",
        "type OperationStatus string",
    ]
    if constants:
        lines.extend(("", "const ("))
        lines.extend(
            f"\t{name} OperationStatus = {_go_string(value)}"
            for name, value in constants
        )
        lines.append(")")
    terminals = set(ir.terminal_status_values)
    terminal_names = [name for name, value in constants if value in terminals]
    lines.extend(
        (
            "",
            "// IsTerminal reports whether the operation state cannot change again.",
            "func (s OperationStatus) IsTerminal() bool {",
        )
    )
    if terminal_names:
        joined = ", ".join(terminal_names)
        lines.extend(("\tswitch s {", f"\tcase {joined}:", "\t\treturn true", "\t}"))
    lines.extend(("\treturn false", "}"))
    lines.extend(
        (
            "",
            "// IsTerminalStatus reports whether status cannot change again.",
            "func IsTerminalStatus(status OperationStatus) bool {",
            "\treturn status.IsTerminal()",
            "}",
        )
    )
    return "\n".join(lines)


def _render_event_types(ir: SpecIR) -> str:
    constants = _unique_constants("OperationEventType", ir.event_type_values)
    lines = [
        "// OperationEventType identifies an operation event payload.",
        "type OperationEventType string",
    ]
    if constants:
        lines.extend(("", "const ("))
        lines.extend(
            f"\t{name} OperationEventType = {_go_string(value)}"
            for name, value in constants
        )
        lines.append(")")
    return "\n".join(lines)


def _render_named_types(ir: SpecIR) -> str:
    model_names = {go_name(model.name) for model in ir.models}
    enums: set[str] = set()
    aliases: set[str] = set()
    for type_ref in _all_type_refs(ir):
        if type_ref.name is None:
            continue
        if type_ref.kind is TypeKind.ENUM:
            enums.add(go_name(type_ref.name))
        elif type_ref.kind is TypeKind.ALIAS:
            aliases.add(go_name(type_ref.name))
    enums.difference_update({"OperationStatus"} | model_names)
    aliases.difference_update({"EventData", "OperationEventType"} | model_names)
    blocks = [
        "// EventData contains typed event data or a raw fallback value.\n"
        "type EventData interface{}"
    ]
    blocks.extend(
        f"// {name} is a named string enum.\ntype {name} string"
        for name in sorted(enums)
    )
    blocks.extend(
        f"// {name} is a named string alias.\ntype {name} string"
        for name in sorted(aliases)
    )
    return "\n\n".join(blocks)


def _decoder_name(model: ModelDef, field_name: str) -> str:
    return "decode" + go_name(model.name) + field_name


def _render_discriminator_decoder(
    model: ModelDef,
    field: FieldDef,
    field_name: str,
) -> str:
    discriminator = field.discriminator
    if discriminator is None:
        raise ValueError(f"{model.name}.{field.name} has no discriminator")
    result_type = _discriminator_base(field)
    function_name = _decoder_name(model, field_name)
    signature = (
        f"func {function_name}(kind string, raw json.RawMessage) "
        f"({result_type}, error) {{"
    )
    lines = [
        signature,
        "\tswitch kind {",
    ]
    for value, type_ref in discriminator.cases:
        target = go_type(type_ref)
        lines.extend(
            (
                f"\tcase {_go_string(value)}:",
                f"\t\tvar value {target}",
                "\t\tif err := json.Unmarshal(raw, &value); err == nil {",
                "\t\t\treturn value, nil",
                "\t\t} else {",
            )
        )
        if discriminator.name == "EventData":
            lines.append("\t\t\tbreak")
        else:
            lines.append("\t\t\treturn nil, err")
        lines.extend(("\t\t}",))
    lines.append("\t}")
    if discriminator.name == "EventData":
        lines.extend(
            (
                "\tvar value any",
                "\tif err := json.Unmarshal(raw, &value); err != nil {",
                "\t\treturn nil, err",
                "\t}",
                "\treturn value, nil",
            )
        )
    else:
        fallback = go_type(discriminator.fallback)
        lines.extend(
            (
                f"\tvar value {fallback}",
                "\tif err := json.Unmarshal(raw, &value); err != nil {",
                "\t\treturn nil, err",
                "\t}",
                "\treturn value, nil",
            )
        )
    lines.append("}")
    return "\n".join(lines)


def _render_marshal_field(
    model: ModelDef,
    field: FieldDef,
    field_name: str,
) -> list[str]:
    value = f"m.{field_name}"
    if ModelTrait.FILE_MODE in model.traits and field.name == "mode":
        value = f"normalized{go_name(model.name)}Mode({value})"
    wire = _go_string(field.wire_name)
    statement = f"putJSONField(object, {wire}, {value})"
    error = _go_string(f"marshal {model.name}.{field.wire_name}: %w")
    body: list[str] = []
    if _field_requires_non_nil(field):
        nil_error = _go_string(
            f"marshal {model.name}.{field.wire_name}: required field is nil"
        )
        body.extend(
            (
                f"if isNilJSONValue(m.{field_name}) {{",
                f"\treturn nil, fmt.Errorf({nil_error})",
                "}",
            )
        )
    body.extend(
        (
            f"if err := {statement}; err != nil {{",
            f"\treturn nil, fmt.Errorf({error}, err)",
            "}",
        )
    )
    if field.required:
        return body
    return [f"if m.{field_name}.IsSet() {{", *["\t" + line for line in body], "}"]


def _render_unmarshal_field(
    model: ModelDef,
    field: FieldDef,
    field_name: str,
) -> list[str]:
    raw_name = "raw" + field_name
    wire = _go_string(field.wire_name)
    error_context = _go_string(f"unmarshal {model.name}.{field.wire_name}: %w")
    lines: list[str] = []
    if field.required:
        missing_error = _go_string(f"missing required field {field.wire_name}")
        lines.extend(
            (
                f"{raw_name}, ok := object[{wire}]",
                "if !ok {",
                f"\treturn fmt.Errorf({missing_error})",
                "}",
            )
        )
        if not field.nullable:
            null_error = _go_string(f"field {field.wire_name} must not be null")
            lines.extend(
                (
                    f"if isJSONNull({raw_name}) {{",
                    f"\treturn fmt.Errorf({null_error})",
                    "}",
                )
            )
    else:
        lines.extend((f"{raw_name}, ok := object[{wire}]", "if ok {"))

    body: list[str]
    if field.discriminator is None:
        unmarshal = (
            f"if err := json.Unmarshal({raw_name}, &decoded.{field_name}); "
            "err != nil {"
        )
        body = [
            unmarshal,
            f"\treturn fmt.Errorf({error_context}, err)",
            "}",
        ]
    else:
        discriminator = field.discriminator
        parent = _go_string(discriminator.parent_field)
        local = field_name[0].lower() + field_name[1:] + "Value"
        body = []
        if field.nullable or not field.required:
            body.extend(
                (
                    f"if isJSONNull({raw_name}) {{",
                    (
                        f"\tdecoded.{field_name} = Null[{_field_base_type(field)}]()"
                        if not field.required
                        else f"\tdecoded.{field_name} = nil"
                    ),
                    "} else {",
                )
            )
            indent = "\t"
        else:
            indent = ""
        decode_call = _decoder_name(model, field_name)
        body.extend(
            (
                f"{indent}kind, err := readDiscriminator(object, {parent})",
                f"{indent}if err != nil {{",
                f"{indent}\treturn fmt.Errorf({error_context}, err)",
                f"{indent}}}",
                f"{indent}{local}, err := {decode_call}(kind, {raw_name})",
                f"{indent}if err != nil {{",
                f"{indent}\treturn fmt.Errorf({error_context}, err)",
                f"{indent}}}",
                (
                    f"{indent}decoded.{field_name} = Some({local})"
                    if not field.required
                    else f"{indent}decoded.{field_name} = {local}"
                ),
            )
        )
        if field.nullable or not field.required:
            body.append("}")

    if field.required:
        lines.extend(body)
    else:
        lines.extend("\t" + line for line in body)
        lines.append("}")
    return lines


def _stream_trait_fields(model: ModelDef, names: list[str]) -> tuple[str, str]:
    by_name = {
        field.name: (field, names[index]) for index, field in enumerate(model.fields)
    }
    try:
        value, value_name = by_name["value"]
        encoding, encoding_name = by_name["encoding"]
    except KeyError as exc:
        raise ValueError(
            f"{model.name}: STREAM_VALUE needs value and encoding fields"
        ) from exc
    for field in (value, encoding):
        if (
            field.type.kind not in (TypeKind.STRING, TypeKind.LITERAL)
            or not field.required
            or field.nullable
        ):
            raise ValueError(
                f"{model.name}: STREAM_VALUE fields must be required strings"
            )
    return value_name, encoding_name


def _render_stream_trait(model: ModelDef, names: list[str]) -> str:
    model_name = go_name(model.name)
    value, encoding = _stream_trait_fields(model, names)
    return f"""// AsBytes decodes the stream payload to bytes.
func (m {model_name}) AsBytes() []byte {{
\tif m.{encoding} == "base64" {{
\t\tvalue, err := base64.StdEncoding.DecodeString(m.{value})
\t\tif err != nil {{
\t\t\treturn []byte{{}}
\t\t}}
\t\treturn value
\t}}
\treturn []byte(m.{value})
}}

// AsText decodes the stream payload to valid UTF-8 text.
func (m {model_name}) AsText() string {{
\tif m.{encoding} == "base64" {{
\t\treturn strings.ToValidUTF8(string(m.AsBytes()), "�")
\t}}
\treturn m.{value}
}}

// New{model_name}FromBytes constructs a stream payload from bytes.
func New{model_name}FromBytes(value []byte) {model_name} {{
\tprintable := true
\tfor _, char := range value {{
\t\tif !((char >= 32 && char < 127) || char == 9 || char == 10 || char == 13) {{
\t\t\tprintable = false
\t\t\tbreak
\t\t}}
\t}}
\tif printable {{
\t\treturn {model_name}{{{value}: string(value), {encoding}: "ascii"}}
\t}}
\treturn {model_name}{{
\t\t{value}: base64.StdEncoding.EncodeToString(value),
\t\t{encoding}: "base64",
\t}}
}}

// New{model_name}FromText constructs a stream payload from text.
func New{model_name}FromText(value string) {model_name} {{
\treturn New{model_name}FromBytes([]byte(value))
}}"""


def _file_mode_field(model: ModelDef, names: list[str]) -> tuple[FieldDef, str]:
    for index, field in enumerate(model.fields):
        if field.name != "mode":
            continue
        if field.nullable or not is_string_integer_union(field.type):
            break
        return field, names[index]
    raise ValueError(f"{model.name}: FILE_MODE needs a string-or-integer mode field")


def _render_file_mode_trait(model: ModelDef, names: list[str]) -> str:
    model_name = go_name(model.name)
    field, field_name = _file_mode_field(model, names)
    if field.required:
        signature = (
            f"func normalized{model_name}Mode(mode StringOrInt64) StringOrInt64 {{"
        )
        normalizer = f"""{signature}
\tif value, ok := mode.Int64Value(); ok {{
\t\treturn StringOrInt64FromString(fmt.Sprintf("%04o", value))
\t}}
\treturn mode
}}"""
        set_value = 'StringOrInt64FromString(fmt.Sprintf("%04o", mode))'
    else:
        normalizer = f"""func normalized{model_name}Mode(
\tmode Optional[StringOrInt64],
) Optional[StringOrInt64] {{
\tmodeValue, present := mode.Value()
\tif !present {{
\t\treturn mode
\t}}
\tif value, ok := modeValue.Int64Value(); ok {{
\t\treturn Some(StringOrInt64FromString(fmt.Sprintf("%04o", value)))
\t}}
\treturn mode
}}"""
        set_value = 'Some(StringOrInt64FromString(fmt.Sprintf("%04o", mode)))'
    return f"""{normalizer}

// SetMode stores mode in the wire-format octal representation.
func (m *{model_name}) SetMode(mode int64) {{
\tm.{field_name} = {set_value}
}}

// NormalizeMode converts an integer mode to its wire-format octal string.
func (m *{model_name}) NormalizeMode() {{
\tm.{field_name} = normalized{model_name}Mode(m.{field_name})
}}"""


def render_model(model: ModelDef) -> str:
    model_name = go_name(model.name)
    names = model_field_names(model)
    nil_error = _go_string(f"unmarshal {model.name} into nil receiver")
    object_error = _go_string(f"{model.name} must be a JSON object")
    unmarshal_error = _go_string(f"unmarshal {model.name}: %w")
    lines = [
        f"// {model_name} is generated from the {model.name} schema.",
        f"type {model_name} struct {{",
    ]
    for index, field in enumerate(model.fields):
        lines.append(f"\t{names[index]} {_field_type(field)}{_struct_tag(field)}")
    lines.append("}")

    lines.extend(
        (
            "",
            f"// MarshalJSON encodes {model_name} with exact wire names.",
            f"func (m {model_name}) MarshalJSON() ([]byte, error) {{",
            f"\tobject := make(map[string]json.RawMessage, {len(model.fields)})",
        )
    )
    for index, field in enumerate(model.fields):
        lines.extend(
            "\t" + line for line in _render_marshal_field(model, field, names[index])
        )
    lines.extend(("\treturn json.Marshal(object)", "}"))

    lines.extend(
        (
            "",
            f"// UnmarshalJSON decodes {model_name} and checks required fields.",
            f"func (m *{model_name}) UnmarshalJSON(data []byte) error {{",
            "\tif m == nil {",
            f"\t\treturn fmt.Errorf({nil_error})",
            "\t}",
            "\tvar object map[string]json.RawMessage",
            "\tif err := json.Unmarshal(data, &object); err != nil {",
            f"\t\treturn fmt.Errorf({unmarshal_error}, err)",
            "\t}",
            "\tif object == nil {",
            f"\t\treturn fmt.Errorf({object_error})",
            "\t}",
            f"\tvar decoded {model_name}",
        )
    )
    for index, field in enumerate(model.fields):
        lines.extend(
            "\t" + line for line in _render_unmarshal_field(model, field, names[index])
        )
    lines.extend(("\t*m = decoded", "\treturn nil", "}"))

    for index, field in enumerate(model.fields):
        if field.discriminator is not None:
            lines.extend(
                ("", _render_discriminator_decoder(model, field, names[index]))
            )
    if ModelTrait.STREAM_VALUE in model.traits:
        lines.extend(("", _render_stream_trait(model, names)))
    if ModelTrait.FILE_MODE in model.traits:
        lines.extend(("", _render_file_mode_trait(model, names)))
    return "\n".join(lines)


def render_models(ir: SpecIR, package_name: str = DEFAULT_PACKAGE_NAME) -> str:
    """Render all model definitions and their JSON support."""
    package_name = validate_package_name(package_name)
    blocks = [
        HEADER.rstrip(),
        f"package {package_name}",
        _render_imports(ir),
        OPTIONAL_SUPPORT.strip(),
        UNION_SUPPORT.strip(),
        MODEL_HELPERS.strip(),
        _render_status(ir),
        _render_event_types(ir),
        _render_named_types(ir),
    ]
    blocks.extend(render_model(model) for model in ir.models)
    return "\n\n".join(blocks) + "\n"


def render_spec_info(ir: SpecIR, package_name: str = DEFAULT_PACKAGE_NAME) -> str:
    """Render build-input provenance constants."""
    package_name = validate_package_name(package_name)
    return (
        f"{HEADER}\n"
        f"package {package_name}\n\n"
        f"const DefaultBaseURL = {_go_string(ir.default_base_url)}\n\n"
        "// SpecSHA256 identifies the exact OpenAPI input document.\n"
        f"const SpecSHA256 = {_go_string(ir.spec_sha256)}\n"
    )


def _run(
    command: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None
) -> None:
    completed = subprocess.run(
        command,
        cwd=cwd,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode == 0:
        return
    detail = completed.stderr.strip() or completed.stdout.strip()
    suffix = f": {detail}" if detail else ""
    raise RuntimeError(f"{' '.join(command)} failed{suffix}")


class GoEmitter(Emitter):
    """Render and validate the generated Go client surface."""

    files = GENERATED_FILES

    def __init__(self, package_name: str = DEFAULT_PACKAGE_NAME) -> None:
        self.package_name = validate_package_name(package_name)

    def render(self, ir: SpecIR) -> dict[str, str]:
        pagination = render_pagination(ir)
        operations = render_operations(ir, self.package_name, extra_source=pagination)

        return {
            "models.gen.go": render_models(ir, self.package_name),
            "operations.gen.go": operations,
            "spec_info.gen.go": render_spec_info(ir, self.package_name),
            "testing.gen.go": render_testing(ir, self.package_name),
        }

    def validate(self, paths: list[Path]) -> None:
        gofmt = shutil.which("gofmt")
        if gofmt is None:
            raise RuntimeError("gofmt is required to validate generated Go code")
        go = shutil.which("go")
        if go is None:
            raise RuntimeError("go is required to validate generated Go code")

        _run([gofmt, "-w", *[str(path) for path in paths]])
        with tempfile.TemporaryDirectory(prefix="contree-go-validate-") as directory:
            module = Path(directory)
            (module / "go.mod").write_text(
                "module generated.invalid/contree\n\ngo 1.23\n",
                encoding="utf-8",
            )
            for path in paths:
                shutil.copy2(path, module / path.name)
            operations = next(
                (path for path in paths if path.name == "operations.gen.go"),
                None,
            )
            if operations is not None and "func (c *Client)" in operations.read_text(
                encoding="utf-8"
            ):
                runtime_root = Path(__file__).resolve().parents[3] / "go" / "v1"
                runtime_files = ["client.go", "errors.go", "runtime.go"]
                if "*eventStream" in operations.read_text(encoding="utf-8"):
                    runtime_files.append("stream.go")
                if "iteratePages[" in operations.read_text(encoding="utf-8"):
                    runtime_files.append("pagination.go")
                for name in runtime_files:
                    source = (runtime_root / name).read_text(encoding="utf-8")
                    if self.package_name != DEFAULT_PACKAGE_NAME:
                        source = source.replace(
                            f"package {DEFAULT_PACKAGE_NAME}",
                            f"package {self.package_name}",
                            1,
                        )
                    (module / name).write_text(source, encoding="utf-8")
            environment = dict(os.environ)
            environment.update({"GOTOOLCHAIN": "local", "GOWORK": "off"})
            _run([go, "test", "./..."], cwd=module, env=environment)


def generate(
    spec_source: str | Path,
    package_dir: Path,
    package_name: str = DEFAULT_PACKAGE_NAME,
) -> Path:
    """Generate the Go client surface."""
    return GoEmitter(package_name).generate(spec_source, package_dir)
