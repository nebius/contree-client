from __future__ import annotations

import pytest

from api_generator.ir import (
    ArgumentPresence,
    BodyKind,
    ModelTrait,
    OperationDef,
    ParameterEncoding,
    ParameterLocation,
    ResponseMode,
    SchemaConverter,
    SpecIR,
    SuccessPolicy,
    TypeKind,
    TypeRef,
    build_ir,
)
from api_generator.loader import Spec, load_spec
from api_generator.python.emitter import render_operation_status


@pytest.fixture(scope="module")
def ir(spec_source: str) -> SpecIR:
    return build_ir(load_spec(spec_source))


def op_by_name(ir: SpecIR, name: str) -> OperationDef:
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
    assert op.response.mode is ResponseMode.SSE
    assert op.response.type == TypeRef(TypeKind.MODEL, name="OperationEvent")
    assert op.response.resume_argument == "last_event_id"
    assert op.request.accept == "text/event-stream"
    arg_names = [arg.name for arg in op.arguments]
    assert arg_names[0] == "operation_id"
    assert "follow" in arg_names
    assert "last_event_id" in arg_names


def test_import_image_returns_uuid(ir: SpecIR) -> None:
    op = op_by_name(ir, "import_image")
    assert op.response.mode is ResponseMode.JSON
    assert op.response.type == TypeRef(TypeKind.STRING)
    assert op.response.json_path == ("uuid",)


def test_subprocess_create_returns_spid(ir: SpecIR) -> None:
    op = op_by_name(ir, "operation_subprocess_create")
    assert op.response.mode is ResponseMode.JSON
    assert op.response.type == TypeRef(TypeKind.INTEGER)
    assert op.response.json_path == ("spid",)


def test_integer_path_param_is_semantic(ir: SpecIR) -> None:
    op = op_by_name(ir, "operation_subprocess")
    spid = next(arg for arg in op.arguments if arg.name == "spid")
    parameter = next(
        param for param in op.request.parameters if param.argument == "spid"
    )
    assert spid.type == TypeRef(TypeKind.INTEGER)
    assert spid.presence is ArgumentPresence.REQUIRED
    assert parameter.location is ParameterLocation.PATH


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
    arguments = {argument.name: argument for argument in op.arguments}
    parameters = {parameter.argument: parameter for parameter in op.request.parameters}
    repeatable_type = TypeRef(
        TypeKind.UNION,
        arguments=(
            TypeRef(TypeKind.STRING),
            TypeRef(
                TypeKind.SEQUENCE,
                arguments=(TypeRef(TypeKind.STRING),),
            ),
        ),
    )

    assert arguments["pattern"].type == repeatable_type
    assert arguments["pattern"].presence is ArgumentPresence.REQUIRED
    for name in ("path", "glob"):
        assert arguments[name].type == repeatable_type
        assert arguments[name].presence is ArgumentPresence.OMIT_IF_NULL
        assert arguments[name].nullable

    for name in ("pattern", "path", "glob"):
        assert parameters[name].location is ParameterLocation.QUERY
        assert parameters[name].encoding is ParameterEncoding.IDENTITY
        assert parameters[name].repeatable


def test_download_has_buffered_and_streaming_bytes_behavior(ir: SpecIR) -> None:
    op = op_by_name(ir, "inspect_image_download")
    assert op.response.mode is ResponseMode.BYTES
    assert op.response.type == TypeRef(TypeKind.BYTES)


def test_check_ops_return_bool(ir: SpecIR) -> None:
    for name in ("check_file_exists", "check_image_file", "check_image_archive"):
        response = op_by_name(ir, name).response
        assert response.mode is ResponseMode.STATUS_BOOL
        assert response.type == TypeRef(TypeKind.BOOLEAN)
        assert response.false_statuses == (404,)


def test_flag_param_is_bool(ir: SpecIR) -> None:
    op = op_by_name(ir, "list_images")
    tagged = next(arg for arg in op.arguments if arg.name == "tagged")
    parameter = next(
        param for param in op.request.parameters if param.argument == "tagged"
    )
    assert tagged.type == TypeRef(TypeKind.BOOLEAN)
    assert tagged.presence is ArgumentPresence.OMIT_IF_FALSE
    assert parameter.location is ParameterLocation.QUERY
    assert parameter.encoding is ParameterEncoding.ONE_IF_TRUE


def test_spawn_instance_flattens_body(ir: SpecIR) -> None:
    op = op_by_name(ir, "spawn_instance")
    arg_names = [arg.name for arg in op.arguments]
    assert arg_names[:2] == ["command", "image"]
    assert "env" in arg_names
    assert "files" in arg_names
    assert op.request.body is not None
    assert op.request.body.kind is BodyKind.JSON_MODEL
    assert op.request.body.model == TypeRef(
        TypeKind.MODEL,
        name="InstanceSpawnRequest",
    )
    assert {binding.argument for binding in op.request.body.bindings} == set(arg_names)


def test_body_arguments_preserve_presence_and_nullability(ir: SpecIR) -> None:
    op = op_by_name(ir, "spawn_instance")
    arguments = {argument.name: argument for argument in op.arguments}

    assert arguments["command"].presence is ArgumentPresence.REQUIRED
    assert not arguments["command"].nullable
    assert arguments["env"].presence is ArgumentPresence.OMIT_IF_UNSET
    assert arguments["env"].nullable
    assert arguments["files"].presence is ArgumentPresence.OMIT_IF_UNSET
    assert not arguments["files"].nullable


def test_inline_body_preserves_nullable_optional_presence() -> None:
    spec = make_synthetic_spec(
        {},
        paths={
            "/sample": {
                "post": {
                    "operationId": "updateSample",
                    "requestBody": {
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "required": ["name"],
                                    "properties": {
                                        "name": {"type": "string"},
                                        "note": {
                                            "type": "string",
                                            "nullable": True,
                                        },
                                    },
                                }
                            }
                        }
                    },
                    "responses": {"204": {"description": "No content"}},
                }
            }
        },
    )
    op = op_by_name(build_ir(spec), "update_sample")
    arguments = {argument.name: argument for argument in op.arguments}

    assert op.request.body is not None
    assert op.request.body.kind is BodyKind.JSON_INLINE
    assert [
        (binding.wire_name, binding.argument) for binding in op.request.body.bindings
    ] == [("name", "name"), ("note", "note")]
    assert arguments["name"].presence is ArgumentPresence.REQUIRED
    assert not arguments["name"].nullable
    assert arguments["note"].presence is ArgumentPresence.OMIT_IF_UNSET
    assert arguments["note"].nullable


def test_binary_body_has_explicit_content_binding(ir: SpecIR) -> None:
    op = op_by_name(ir, "upload_file")
    content = next(argument for argument in op.arguments if argument.name == "content")

    assert content.type == TypeRef(
        TypeKind.UNION,
        arguments=(
            TypeRef(TypeKind.BYTES),
            TypeRef(TypeKind.BINARY_STREAM),
        ),
    )
    assert content.presence is ArgumentPresence.REQUIRED
    assert op.request.body is not None
    assert op.request.body.kind is BodyKind.BINARY
    assert [
        (binding.wire_name, binding.argument) for binding in op.request.body.bindings
    ] == [("content", "content")]


def test_parameter_encodings_are_semantic(ir: SpecIR) -> None:
    list_images = op_by_name(ir, "list_images")
    parameters = {
        parameter.argument: parameter for parameter in list_images.request.parameters
    }
    assert parameters["limit"].encoding is ParameterEncoding.STRING
    assert parameters["since"].encoding is ParameterEncoding.TIME
    assert parameters["tagged"].encoding is ParameterEncoding.ONE_IF_TRUE

    events = op_by_name(ir, "iter_operation_events")
    last_event_id = next(
        parameter
        for parameter in events.request.parameters
        if parameter.argument == "last_event_id"
    )
    assert last_event_id.wire_name == "Last-Event-Id"
    assert last_event_id.location is ParameterLocation.HEADER
    assert last_event_id.encoding is ParameterEncoding.STRING


def test_stream_response_modes_are_semantic(ir: SpecIR) -> None:
    download = op_by_name(ir, "inspect_image_download")
    archive = op_by_name(ir, "inspect_image_archive")
    events = op_by_name(ir, "iter_operation_events")

    assert download.response.mode is ResponseMode.BYTES
    assert download.response.type == TypeRef(TypeKind.BYTES)
    assert archive.response.mode is ResponseMode.BYTE_STREAM
    assert archive.response.type == TypeRef(TypeKind.BYTES)
    assert events.response.mode is ResponseMode.SSE
    assert events.response.type == TypeRef(TypeKind.MODEL, name="OperationEvent")


@pytest.mark.parametrize(
    ("operation_name", "iterator_name", "item_name", "items_path"),
    (
        ("list_images", "iter_images", "Image", ("images",)),
        ("list_files", "iter_files", "File", ("files",)),
        ("list_operations", "iter_operations", "OperationSummary", ()),
    ),
)
def test_pagination_is_part_of_operation_ir(
    ir: SpecIR,
    operation_name: str,
    iterator_name: str,
    item_name: str,
    items_path: tuple[str, ...],
) -> None:
    pagination = op_by_name(ir, operation_name).pagination

    assert pagination is not None
    assert pagination.iterator_name == iterator_name
    assert pagination.item_type == TypeRef(TypeKind.MODEL, name=item_name)
    assert pagination.items_path == items_path
    assert pagination.limit_argument == "limit"
    assert pagination.offset_argument == "offset"
    assert pagination.max_page_size == 1000


def test_text_param_skipped(ir: SpecIR) -> None:
    op = op_by_name(ir, "inspect_image_list")
    assert "text" not in [arg.name for arg in op.arguments]


def test_nested_models_synthesized(ir: SpecIR) -> None:
    names = set(ir.model_names)
    assert "FileSpec" in names
    assert "ImageImportRegistry" in names
    assert "OperationResult" in names
    assert "InstanceResultState" in names


def test_operation_instance_metadata_merged(ir: SpecIR) -> None:
    model = next(
        model for model in ir.models if model.name == "OperationInstanceMetadata"
    )
    field_names = {field.name for field in model.fields}
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


def synthetic_fields(schemas: dict, name: str = "Sample") -> dict[str, TypeRef]:
    spec = make_synthetic_spec(schemas)
    converter = SchemaConverter(spec)  # type: ignore[arg-type]
    converter.convert_object(name, spec.schemas[name])  # type: ignore[attr-defined]
    model = converter.model_by_name(name)
    return {field.name: field.type for field in model.fields}


def test_field_name_is_separate_from_wire_name() -> None:
    spec = make_synthetic_spec(
        {
            "Sample": {
                "type": "object",
                "properties": {"wireName": {"type": "string"}},
            }
        }
    )
    converter = SchemaConverter(spec)  # type: ignore[arg-type]
    converter.convert_object("Sample", spec.schemas["Sample"])  # type: ignore[attr-defined]
    (field,) = converter.model_by_name("Sample").fields

    assert field.name == "wire_name"
    assert field.wire_name == "wireName"


def test_scalar_types_are_semantic() -> None:
    fields = synthetic_fields(
        {
            "Sample": {
                "type": "object",
                "properties": {
                    "anything": {},
                    "text": {"type": "string"},
                    "count": {"type": "integer"},
                    "ratio": {"type": "number"},
                    "enabled": {"type": "boolean"},
                    "created_at": {"type": "string", "format": "date-time"},
                    "choice": {"type": "string", "enum": ["first", "second"]},
                },
            }
        }
    )

    assert fields == {
        "anything": TypeRef(TypeKind.ANY),
        "text": TypeRef(TypeKind.STRING),
        "count": TypeRef(TypeKind.INTEGER),
        "ratio": TypeRef(TypeKind.NUMBER),
        "enabled": TypeRef(TypeKind.BOOLEAN),
        "created_at": TypeRef(TypeKind.DATETIME),
        "choice": TypeRef(TypeKind.LITERAL, values=("first", "second")),
    }


def test_collection_types_have_semantic_arguments() -> None:
    child = {
        "type": "object",
        "properties": {"name": {"type": "string"}},
    }
    fields = synthetic_fields(
        {
            "Child": child,
            "Sample": {
                "type": "object",
                "properties": {
                    "names": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "children": {
                        "type": "array",
                        "items": {"$ref": "#/components/schemas/Child"},
                    },
                    "flags": {
                        "type": "object",
                        "additionalProperties": {"type": "boolean"},
                    },
                    "attributes": {"type": "object"},
                },
            },
        }
    )

    assert fields["names"] == TypeRef(
        TypeKind.LIST,
        arguments=(TypeRef(TypeKind.STRING),),
    )
    assert fields["children"] == TypeRef(
        TypeKind.LIST,
        arguments=(TypeRef(TypeKind.MODEL, name="Child"),),
    )
    assert fields["flags"] == TypeRef(
        TypeKind.MAP,
        arguments=(TypeRef(TypeKind.BOOLEAN),),
    )
    assert fields["attributes"] == TypeRef(
        TypeKind.MAP,
        arguments=(TypeRef(TypeKind.ANY),),
    )


def test_named_model_enum_and_alias_types_are_distinct() -> None:
    fields = synthetic_fields(
        {
            "Child": {
                "type": "object",
                "properties": {"name": {"type": "string"}},
            },
            "Sample": {
                "type": "object",
                "properties": {
                    "child": {"$ref": "#/components/schemas/Child"},
                    "status": {
                        "type": "string",
                        "enum": ["SUCCESS", "FAILED"],
                    },
                    "event_type": {"$ref": "#/components/schemas/OperationEventType"},
                },
            },
        }
    )

    assert fields["child"] == TypeRef(TypeKind.MODEL, name="Child")
    assert fields["status"] == TypeRef(TypeKind.ENUM, name="OperationStatus")
    assert fields["event_type"] == TypeRef(
        TypeKind.ALIAS,
        name="OperationEventType",
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
    sample = converter.model_by_name("Sample")
    fields = {field.name: field for field in sample.fields}
    assert fields["one_of"].type == TypeRef(
        TypeKind.UNION,
        arguments=(TypeRef(TypeKind.NUMBER), TypeRef(TypeKind.STRING)),
    )
    assert fields["any_of"].type == TypeRef(
        TypeKind.UNION,
        arguments=(TypeRef(TypeKind.BOOLEAN), TypeRef(TypeKind.INTEGER)),
    )


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
    file_item = next(model for model in ir.models if model.name == "FileItem")
    fields = {field.name: field for field in file_item.fields}
    expected = TypeRef(
        TypeKind.UNION,
        arguments=(TypeRef(TypeKind.STRING), TypeRef(TypeKind.INTEGER)),
    )
    assert fields["owner"].type == expected
    assert fields["group"].type == expected


def test_operation_response_metadata_has_semantic_discriminator(ir: SpecIR) -> None:
    operation_response = next(
        model for model in ir.models if model.name == "OperationResponse"
    )
    metadata = next(
        field for field in operation_response.fields if field.name == "metadata"
    )

    instance = TypeRef(TypeKind.MODEL, name="OperationInstanceMetadata")
    image_import = TypeRef(TypeKind.MODEL, name="ImageImportMetadata")
    assert metadata.type == TypeRef(
        TypeKind.UNION,
        arguments=(instance, image_import),
    )
    assert metadata.discriminator is not None
    assert metadata.discriminator.parent_field == "kind"
    assert metadata.discriminator.cases == (("instance", instance),)
    assert metadata.discriminator.fallback == image_import
    assert metadata.discriminator.name is None


def test_operation_event_data_has_semantic_discriminator(ir: SpecIR) -> None:
    operation_event = next(
        model for model in ir.models if model.name == "OperationEvent"
    )
    data = next(field for field in operation_event.fields if field.name == "data")
    raw_payload = TypeRef(
        TypeKind.MAP,
        arguments=(TypeRef(TypeKind.ANY),),
    )

    assert data.type == TypeRef(
        TypeKind.UNION,
        arguments=(TypeRef(TypeKind.ALIAS, name="EventData"), raw_payload),
    )
    assert data.discriminator is not None
    assert data.discriminator.parent_field == "type"
    assert data.discriminator.name == "EventData"
    assert data.discriminator.fallback == raw_payload
    cases = dict(data.discriminator.cases)
    assert cases["stdout"] == TypeRef(TypeKind.MODEL, name="EventDataStream")
    assert cases["completion"] == TypeRef(
        TypeKind.MODEL,
        name="EventDataCompletion",
    )


def test_model_traits_describe_shared_semantics(ir: SpecIR) -> None:
    models = {model.name: model for model in ir.models}

    assert models["StreamRepr"].traits == frozenset({ModelTrait.STREAM_VALUE})
    assert models["EventDataStream"].traits == frozenset({ModelTrait.STREAM_VALUE})
    assert models["FileSpec"].traits == frozenset({ModelTrait.FILE_MODE})
    assert models["OperationEvent"].traits == frozenset()


def test_find_image_by_tag_returns_image_uuid(ir: SpecIR) -> None:
    op = op_by_name(ir, "inspect_find_image_by_tag")
    assert op.response.mode is ResponseMode.LOCATION
    assert op.response.type == TypeRef(TypeKind.STRING)
    assert op.response.success is SuccessPolicy.EXACT
    assert op.response.success_statuses == (302,)
    assert op.response.header_name == "location"


def test_operation_status_renders_zero_one_many_sets() -> None:
    """P2-10: 0/1/N terminal statuses must all render valid Python."""

    def ir_with(statuses: list[str], terminal: list[str]) -> SpecIR:
        return SpecIR(
            default_base_url="",
            spec_text="",
            spec_sha256="",
            models=[],
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
