"""JS emitter output checks that need no generated package on disk."""

from __future__ import annotations

from api_generator.ir import (
    ArgumentDef,
    ArgumentPresence,
    BodyBinding,
    BodyDef,
    BodyKind,
    FieldDef,
    ModelDef,
    OperationDef,
    PaginationDef,
    ParameterDef,
    ParameterLocation,
    RequestDef,
    ResponseDef,
    ResponseMode,
    SpecIR,
    TypeKind,
    TypeRef,
    build_ir,
)
from api_generator.js.emitter import (
    method_call_args,
    op_signature,
    render_build_fn,
    render_class_dts,
    render_class_js,
    render_client_dts,
    render_client_js,
    render_client_method,
    render_op_reference,
    render_parse_fn,
    ts_params,
)
from api_generator.loader import load_spec


def test_reserved_word_param_is_aliased(spec_source: str) -> None:
    """``case`` stays the options key but cannot be a bare JS local."""
    ir = build_ir(load_spec(spec_source))
    op = next(o for o in ir.operations if o.name == "inspect_image_grep")
    src = render_build_fn(op)
    assert "case: case_" in src
    assert 'query["case"] = case_;' in src


def test_repeatable_query_params_accept_arrays() -> None:
    repeatable_string = TypeRef(
        TypeKind.UNION,
        arguments=(
            TypeRef(TypeKind.STRING),
            TypeRef(
                TypeKind.SEQUENCE,
                arguments=(TypeRef(TypeKind.STRING),),
            ),
        ),
    )
    op = OperationDef(
        name="inspect_image_grep",
        http_method="GET",
        path="/inspect/{image_uuid}/grep",
        summary="",
        arguments=[
            ArgumentDef(
                "pattern",
                repeatable_string,
                ArgumentPresence.REQUIRED,
            ),
            ArgumentDef(
                "path",
                repeatable_string,
                ArgumentPresence.OMIT_IF_NULL,
                nullable=True,
            ),
            ArgumentDef(
                "glob",
                repeatable_string,
                ArgumentPresence.OMIT_IF_NULL,
                nullable=True,
            ),
        ],
    )

    assert "pattern: string | readonly string[]" in ts_params(op)
    assert "path?: string | readonly string[]" in ts_params(op)
    assert "glob?: string | readonly string[]" in ts_params(op)


def test_scalar_field_response_unwrapped(spec_source: str) -> None:
    ir = build_ir(load_spec(spec_source))
    op = next(o for o in ir.operations if o.name == "operation_subprocess_create")
    src = render_parse_fn(op)
    assert src is not None
    assert 'Number(jsonObject(response)["spid"])' in src


def test_reserved_word_required_arg_stays_consistent() -> None:
    """A required argument named like a reserved word must carry the
    same alias through the signature, call site, .d.ts declaration,
    doc signature and builder body (the current spec has no such
    parameter, so this is a synthetic operation)."""
    op = OperationDef(
        name="synthetic_op",
        http_method="GET",
        path="/things/{default}",
        summary="",
        arguments=[
            ArgumentDef(
                "default",
                TypeRef(TypeKind.STRING),
                ArgumentPresence.REQUIRED,
            )
        ],
        request=RequestDef(
            parameters=[
                ParameterDef(
                    "default",
                    "default",
                    ParameterLocation.PATH,
                )
            ]
        ),
    )
    assert method_call_args(op) == "default_"
    assert op_signature(op) == "default_"
    assert ts_params(op) == "default_: string"
    src = render_build_fn(op)
    assert "function buildSyntheticOp(default_)" in src
    assert "${quotePath(default_)}" in src


def test_model_uses_camel_case_and_exact_wire_names() -> None:
    model = ModelDef(
        name="Sample",
        description="",
        fields=[
            FieldDef(
                name="wire_name",
                wire_name="wireName",
                type=TypeRef(TypeKind.STRING),
                required=True,
                nullable=False,
            ),
            FieldDef(
                name="created_at",
                wire_name="created_at",
                type=TypeRef(TypeKind.STRING),
                required=True,
                nullable=False,
            ),
        ],
    )
    source = render_class_js(model)
    declarations = render_class_dts(model)

    assert "this.wireName = fields.wireName;" in source
    assert "this.createdAt = fields.createdAt;" in source
    assert 'wireName: data["wireName"]' in source
    assert 'createdAt: data["created_at"]' in source
    assert 'data["wireName"] = this.wireName;' in source
    assert 'data["created_at"] = this.createdAt;' in source
    assert "wire_name" not in source
    assert "wireName: string;" in declarations
    assert "createdAt: string;" in declarations
    assert "wire_name" not in declarations


def test_operation_uses_camel_case_and_exact_wire_names() -> None:
    op = OperationDef(
        name="synthetic_op",
        http_method="GET",
        path="/things/{operationId}",
        summary="",
        arguments=[
            ArgumentDef(
                "operation_id",
                TypeRef(TypeKind.STRING),
                ArgumentPresence.REQUIRED,
            ),
            ArgumentDef(
                "last_event_id",
                TypeRef(TypeKind.INTEGER),
                ArgumentPresence.OMIT_IF_NULL,
                nullable=True,
            ),
            ArgumentDef(
                "max_count",
                TypeRef(TypeKind.INTEGER),
                ArgumentPresence.OMIT_IF_NULL,
                nullable=True,
            ),
            ArgumentDef(
                "wire_name",
                TypeRef(TypeKind.STRING),
                ArgumentPresence.OMIT_IF_UNSET,
            ),
        ],
        request=RequestDef(
            parameters=[
                ParameterDef(
                    "operationId",
                    "operation_id",
                    ParameterLocation.PATH,
                ),
                ParameterDef(
                    "Last-Event-Id",
                    "last_event_id",
                    ParameterLocation.HEADER,
                ),
                ParameterDef(
                    "max_count",
                    "max_count",
                    ParameterLocation.QUERY,
                ),
            ],
            body=BodyDef(
                BodyKind.JSON_INLINE,
                bindings=[BodyBinding("wire_name", "wire_name")],
            ),
        ),
    )

    source = render_build_fn(op)
    assert (
        "operationId, { lastEventId = null, maxCount = null, wireName } = {}" in source
    )
    assert "${quotePath(operationId)}" in source
    assert 'headers["Last-Event-Id"] = String(lastEventId);' in source
    assert 'query["max_count"] = maxCount;' in source
    assert 'payload["wire_name"] = wireName;' in source
    declarations = ts_params(op)
    assert "operationId: string" in declarations
    assert "lastEventId?: number | null" in declarations
    assert "maxCount?: number | null" in declarations
    assert "wireName?: string | undefined" in declarations
    assert ":param options.lastEventId:" in render_op_reference(op)


def test_stream_and_pagination_options_use_camel_case() -> None:
    stream = OperationDef(
        name="iter_events",
        http_method="GET",
        path="/events",
        summary="",
        arguments=[
            ArgumentDef(
                "last_event_id",
                TypeRef(TypeKind.INTEGER),
                ArgumentPresence.OMIT_IF_NULL,
                nullable=True,
            )
        ],
        response=ResponseDef(
            ResponseMode.SSE,
            type=TypeRef(TypeKind.MODEL, name="SampleEvent"),
            resume_argument="last_event_id",
        ),
    )
    assert "options.lastEventId" in "\n".join(render_client_method(stream))

    listing = OperationDef(
        name="list_samples",
        http_method="GET",
        path="/samples",
        summary="",
        response=ResponseDef(
            ResponseMode.JSON,
            type=TypeRef(TypeKind.MODEL, name="SamplePage"),
        ),
        pagination=PaginationDef(
            iterator_name="iter_samples",
            item_type=TypeRef(TypeKind.MODEL, name="Sample"),
            items_path=("result_items",),
            limit_argument="limit",
            offset_argument="offset",
            max_page_size=100,
        ),
    )
    ir = SpecIR(
        default_base_url="",
        spec_text="",
        spec_sha256="",
        models=[],
        operations=[listing],
        event_type_values=[],
        status_values=[],
        terminal_status_values=[],
    )
    source = render_client_js(ir)
    assert "{ pageSize = 100, limit = null, ...filters }" in source
    assert "const page = response.resultItems ?? [];" in source
    assert "{ lastEventId = null, spid = null, since = null, timeout = null }" in source
    assert "lastEventId: lastId" in source
    declarations = render_client_dts(ir)
    assert "pageSize?: number;" in declarations
    assert "lastEventId?: number | null;" in declarations
