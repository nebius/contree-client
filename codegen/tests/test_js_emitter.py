"""JS emitter output checks that need no generated package on disk."""

from __future__ import annotations

from api_generator.ir import ArgDef, OpDef, ParamDef, build_ir
from api_generator.js.emitter import (
    method_call_args,
    op_signature,
    render_build_fn,
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
    op = OpDef(
        name="synthetic_op",
        http_method="GET",
        path="/things/{default}",
        summary="",
        args=[ArgDef("default", "str", None)],
        params=[ParamDef("default", "default", "path", "str", required=True)],
    )
    assert method_call_args(op) == "default_"
    assert op_signature(op) == "default_"
    assert ts_params(op) == "default_: string"
    src = render_build_fn(op)
    assert "function buildSyntheticOp(default_)" in src
    assert "${quotePath(default_)}" in src
