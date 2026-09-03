"""The generated JS API reference page covers the whole surface."""

from __future__ import annotations

from pathlib import Path

from api_generator.ir import (
    ArgumentDef,
    ArgumentPresence,
    FieldDef,
    ModelDef,
    OperationDef,
    ResponseMode,
    SpecIR,
    TypeKind,
    TypeRef,
    build_ir,
)
from api_generator.js.emitter import JsEmitter, camel, render_reference
from api_generator.loader import load_spec


def test_reference_covers_every_operation_and_model(spec_source: str) -> None:
    ir = build_ir(load_spec(spec_source))
    page = render_reference(ir)
    for op in ir.operations:
        assert f".. js:method:: ContreeClient.{camel(op.name)}(" in page, op.name
        if op.response.mode is ResponseMode.BYTES:
            assert camel(f"{op.name}_stream") in page
    for model in ir.models:
        assert f".. js:class:: {model.name}(" in page, model.name
    assert ir.spec_sha256[:12] in page  # provenance line
    # the helpers section documents the hand-written surface
    for helper in ("waitOperation", "followOperationEvents", "ensureFile"):
        assert helper in page


def test_generate_into_temporary_package_skips_docs(
    spec_source: str, tmp_path: Path
) -> None:
    """Publishing the reference requires the repo docs layout; a
    temporary package (tests, sandboxes) must not invent one."""
    package_dir = tmp_path / "client-js" / "lib"
    JsEmitter().generate(spec_source, package_dir)
    assert not (tmp_path / "docs").exists()


def test_reference_uses_public_names_but_keeps_wire_query_names() -> None:
    model = ModelDef(
        "Result",
        "Read `state.exit_code` after completion.",
        [FieldDef("exit_code", "exit_code", TypeRef(TypeKind.INTEGER), True, False)],
    )
    operation = OperationDef(
        "search",
        "GET",
        "/search",
        "Search",
        description=(
            "Share max_total via `max_total`; raw query is `?max_total=N` and "
            'wire JSON is `{"max_total": 1}`.'
        ),
        arguments=[
            ArgumentDef(
                "max_total",
                TypeRef(TypeKind.INTEGER),
                ArgumentPresence.OMIT_IF_NULL,
            )
        ],
    )
    page = render_reference(SpecIR("", "", "", [model], [operation], [], [], []))

    assert "``state.exitCode``" in page
    assert "Share maxTotal" in page
    assert "via ``maxTotal``;" in page
    assert "``?max_total=N``" in page
    assert '``{"max_total": 1}``' in page
