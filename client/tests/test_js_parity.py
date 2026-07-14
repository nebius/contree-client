"""Cross-language provenance: Python and JS must share one spec."""

from __future__ import annotations

import importlib
import re
from pathlib import Path
from types import ModuleType

import pytest

JS_SPEC_INFO = (
    Path(__file__).resolve().parent.parent.parent / "client-js" / "lib" / "specInfo.js"
)


def test_python_and_js_are_built_from_the_same_spec(
    generated_package: ModuleType,
) -> None:
    """P2-12: the two generated packages must embed the same OpenAPI
    digest - a release built from two different snapshots is a bug."""
    if not JS_SPEC_INFO.exists():
        pytest.skip("the JS package is not generated (run `make generate-js`)")
    spec_info = importlib.import_module("contree_client.spec_info")
    match = re.search(r'SPEC_SHA256 =\s*"([0-9a-f]{64})"', JS_SPEC_INFO.read_text())
    assert match is not None, "no SPEC_SHA256 in client-js/lib/specInfo.js"
    assert match.group(1) == spec_info.SPEC_SHA256
