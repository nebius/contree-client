"""The generated JS API reference page covers the whole surface."""

from __future__ import annotations

from pathlib import Path

from api_generator.ir import build_ir
from api_generator.js.emitter import JsEmitter, camel, render_reference
from api_generator.loader import load_spec


def test_reference_covers_every_operation_and_model(spec_source: str) -> None:
    ir = build_ir(load_spec(spec_source))
    page = render_reference(ir)
    for op in ir.operations:
        assert f".. js:method:: ContreeClient.{camel(op.name)}(" in page, op.name
        if op.stream_variant:
            assert camel(f"{op.name}_stream") in page
    for cls in ir.classes:
        assert f".. js:class:: {cls.name}(" in page, cls.name
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
