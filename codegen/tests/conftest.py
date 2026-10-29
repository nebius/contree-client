"""Fixtures for the code-generator test suite.

These tests exercise the builder itself (loader, IR, both emitters,
the packaging gate) and always require the generator and the OpenAPI
spec - they run on the build machine only, never from an artifact.
"""

from __future__ import annotations

import importlib
import os
from pathlib import Path
from types import ModuleType

import pytest

from api_generator.loader import load_spec
from api_generator.python.emitter import generate

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
CLIENT_ROOT = REPO_ROOT / "client"


@pytest.fixture(scope="session")
def spec_source(tmp_path_factory: pytest.TempPathFactory) -> str:
    """The OpenAPI spec location as a local path.

    CONTREE_SPEC overrides the committed snapshot. A URL is fetched
    once per session.
    """
    source = os.environ.get("CONTREE_SPEC") or str(REPO_ROOT / "api/v1/spec.yaml")
    if source.startswith(("http://", "https://")):
        path = tmp_path_factory.mktemp("spec") / "api.yaml"
        path.write_text(load_spec(source).text, encoding="utf-8")
        return str(path)
    return source


@pytest.fixture(scope="session")
def generated_package(spec_source: str) -> ModuleType:
    """Generate the Python package into the source tree and import it."""
    generate(spec_source, CLIENT_ROOT / "contree_client")
    return importlib.import_module("contree_client")
