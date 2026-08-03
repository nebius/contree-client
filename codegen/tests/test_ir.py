from __future__ import annotations

import pytest

from api_generator.ir import OpDef, SchemaConverter, SpecIR, build_ir
from api_generator.loader import Spec, load_spec
from api_generator.python.emitter import render_operation_status


@pytest.fixture(scope="module")
def ir(spec_source: str) -> SpecIR:
    return build_ir(load_spec(spec_source))


def op_by_name(ir: SpecIR, name: str) -> OpDef:
    for op in ir.operations:
        if op.name == name:
            return op
    raise AssertionError(f"operation {name!r} not generated")


def test_expected_operations_present(ir: SpecIR) -> None:
    names = {op.name for op in ir.operations}
    assert {
        "list_images",
        "delete_image_tag",
        "update_image_tag",
        "import_image",
        "upload_file",
        "list_files",
        "get_file",
        "check_file_exists",
        "spawn_instance",
        "list_operations",
        "get_operation_status",
        "cancel_operation",
        "iter_operation_events",
        "inspect_find_image_by_tag",
        "inspect_image",
        "inspect_image_download",
        "check_image_file",
        "inspect_image_archive",
        "check_image_archive",
        "inspect_image_list",
        "whoami",
    } <= names


def test_redirect_operations_skipped(ir: SpecIR) -> None:
    names = {op.name for op in ir.operations}
    assert "inspect_redirect" not in names
    assert "inspect_image_redirect" not in names


def test_sse_operation(ir: SpecIR) -> None:
    op = op_by_name(ir, "iter_operation_events")
    assert op.kind == "sse"
    arg_names = [arg.py_name for arg in op.args]
    assert arg_names[0] == "operation_id"
    assert "follow" in arg_names
    assert "last_event_id" in arg_names


def test_import_image_returns_uuid(ir: SpecIR) -> None:
    op = op_by_name(ir, "import_image")
    assert op.return_annotation == "str"
    assert 'json_object(response)["uuid"]' in op.parse_src


def test_subprocess_create_returns_spid(ir: SpecIR) -> None:
    op = op_by_name(ir, "operation_subprocess_create")
    assert op.return_annotation == "int"
    assert 'int(json_object(response)["spid"])' in op.parse_src


def test_integer_path_param_annotated_int(ir: SpecIR) -> None:
    op = op_by_name(ir, "operation_subprocess")
    spid = next(arg for arg in op.args if arg.py_name == "spid")
    assert spid.annotation == "int"


def test_grep_repeatable_query_params_accept_sequences() -> None:
    spec = make_synthetic_spec(
        {},
        paths={
            "/inspect/{image_uuid}/grep": {
                "get": {
                    "operationId": "inspectImageGrep",
                    "parameters": [
                        {
                            "name": "image_uuid",
                            "in": "path",
                            "required": True,
                            "schema": {"type": "string"},
                        },
                        {
                            "name": "pattern",
                            "in": "query",
                            "required": True,
                            "schema": {"type": "string"},
                        },
                        {
                            "name": "path",
                            "in": "query",
                            "schema": {"type": "string"},
                        },
                        {
                            "name": "glob",
                            "in": "query",
                            "schema": {"type": "string"},
                        },
                    ],
                    "responses": {"204": {"description": "No content"}},
                }
            }
        },
    )
    op = op_by_name(build_ir(spec), "inspect_image_grep")
    args = {arg.py_name: arg for arg in op.args}
    params = {param.py_name: param for param in op.params}

    assert args["pattern"].annotation == "str | Sequence[str]"
    for name in ("path", "glob"):
        assert args[name].annotation == "str | Sequence[str] | None"

    for name in ("pattern", "path", "glob"):
        assert params[name].repeatable

    assert "query: dict[str, str | Sequence[str]] = {}" in op.build_src


def test_download_has_stream_variant(ir: SpecIR) -> None:
    op = op_by_name(ir, "inspect_image_download")
    assert op.kind == "bytes"
    assert op.stream_variant


def test_check_ops_return_bool(ir: SpecIR) -> None:
    for name in ("check_file_exists", "check_image_file", "check_image_archive"):
        assert op_by_name(ir, name).return_annotation == "bool"


def test_flag_param_is_bool(ir: SpecIR) -> None:
    op = op_by_name(ir, "list_images")
    tagged = next(arg for arg in op.args if arg.py_name == "tagged")
    assert tagged.annotation == "bool"
    assert tagged.default == "False"


def test_spawn_instance_flattens_body(ir: SpecIR) -> None:
    op = op_by_name(ir, "spawn_instance")
    arg_names = [arg.py_name for arg in op.args]
    assert arg_names[:2] == ["command", "image"]
    assert "env" in arg_names
    assert "files" in arg_names
    assert "InstanceSpawnRequest(" in op.build_src


def test_text_param_skipped(ir: SpecIR) -> None:
    op = op_by_name(ir, "inspect_image_list")
    assert "text" not in [arg.py_name for arg in op.args]


def test_nested_classes_synthesized(ir: SpecIR) -> None:
    names = set(ir.class_names)
    assert "FileSpec" in names
    assert "ImageImportRegistry" in names
    assert "OperationResult" in names
    assert "InstanceResultState" in names


def test_operation_instance_metadata_merged(ir: SpecIR) -> None:
    cls = next(c for c in ir.classes if c.name == "OperationInstanceMetadata")
    field_names = {f.py_name for f in cls.fields}
    assert {"command", "image", "result", "env"} <= field_names


def make_synthetic_spec(schemas: dict, *, paths: dict | None = None) -> Spec:
    status_enum = {"type": "string", "enum": ["SUCCESS", "FAILED"]}
    return Spec(
        {
            "info": {"version": "0"},
            "servers": [{"variables": {"baseUrl": {"default": "https://x.dev"}}}],
            "paths": paths or {},
            "components": {
                "schemas": {
                    "OperationSummary": {
                        "type": "object",
                        "properties": {"status": status_enum},
                    },
                    "EventDataCompletion": {
                        "type": "object",
                        "properties": {"status": status_enum},
                    },
                    "OperationEventType": {
                        "type": "string",
                        "enum": ["init"],
                    },
                    **schemas,
                }
            },
        },
        "",
    )


def test_scalar_unions_generalized() -> None:
    spec = make_synthetic_spec(
        {
            "Sample": {
                "type": "object",
                "properties": {
                    "one_of": {"oneOf": [{"type": "number"}, {"type": "string"}]},
                    "any_of": {"anyOf": [{"type": "boolean"}, {"type": "integer"}]},
                },
            }
        }
    )
    converter = SchemaConverter(spec)  # type: ignore[arg-type]
    converter.convert_object("Sample", spec.schemas["Sample"])  # type: ignore[attr-defined]
    sample = converter.class_by_name("Sample")
    fields = {field.py_name: field for field in sample.fields}
    assert fields["one_of"].type.annotation == "float | str"
    assert fields["any_of"].type.annotation == "bool | int"


def test_object_union_without_override_fails_fast() -> None:
    spec = make_synthetic_spec(
        {
            "Sample": {
                "type": "object",
                "properties": {
                    "payload": {
                        "oneOf": [
                            {"type": "object"},
                            {"type": "string"},
                        ]
                    }
                },
            }
        }
    )
    converter = SchemaConverter(spec)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="unsupported oneOf"):
        converter.convert_object("Sample", spec.schemas["Sample"])  # type: ignore[attr-defined]


def test_file_item_union_still_generic(ir: SpecIR) -> None:
    file_item = next(cls for cls in ir.classes if cls.name == "FileItem")
    fields = {field.py_name: field for field in file_item.fields}
    assert fields["owner"].type.annotation == "str | int"
    assert fields["group"].type.annotation == "str | int"


def test_find_image_by_tag_returns_image_uuid(ir: SpecIR) -> None:
    op = op_by_name(ir, "inspect_find_image_by_tag")
    assert op.return_annotation == "str"
    assert 'response.headers["location"]' in op.parse_src
    assert 'rsplit("/", 1)[-1]' in op.parse_src


def test_operation_status_renders_zero_one_many_sets() -> None:
    """P2-10: 0/1/N terminal statuses must all render valid Python."""

    def ir_with(statuses: list[str], terminal: list[str]) -> SpecIR:
        return SpecIR(
            default_base_url="",
            spec_text="",
            spec_sha256="",
            classes=[],
            operations=[],
            event_type_values=[],
            status_values=statuses,
            terminal_status_values=terminal,
        )

    for statuses, terminal in (
        (["RUNNING"], []),
        (["RUNNING", "SUCCESS"], ["SUCCESS"]),
        (["A", "B", "C"], ["B", "C"]),
        (["SUCCESS"], ["SUCCESS"]),  # empty ACTIVE set
    ):
        source = render_operation_status(ir_with(statuses, terminal))
        compile(source, "<generated>", "exec")  # SyntaxError = failure
        assert "frozenset((,))" not in source
