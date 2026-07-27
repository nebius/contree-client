"""JS emitter output checks that need no generated package on disk."""

from __future__ import annotations

from api_generator.ir import build_ir
from api_generator.js.emitter import render_build_fn, render_parse_fn
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
