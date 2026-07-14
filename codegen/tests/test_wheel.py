"""Artifact smoke: the built wheel must be installable and complete.

Regression for improvements.md P1-02: uv_build's namespace mode (which
lets the workspace sync on a fresh clone before generation) would
happily package a tree *without* the generated modules into a
formally successful wheel. Building here and importing the public
modules from an isolated environment makes that impossible to miss.
"""

from __future__ import annotations

import pathlib
import shutil
import subprocess
from pathlib import Path
from types import ModuleType

import pytest

CLIENT_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent / "client"

# what a bare install (no extras) guarantees to import
SMOKE_IMPORTS = (
    "contree_client",
    "contree_client.http",
    "contree_client.models",
    "contree_client.sync",
    "contree_client.testing",
)


def run(uv: str, *args: str) -> None:
    result = subprocess.run(
        [uv, *args],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_wheel_builds_and_imports_in_isolation(
    generated_package: ModuleType, tmp_path: Path
) -> None:
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv is not installed")

    run(uv, "build", str(CLIENT_ROOT), "--out-dir", str(tmp_path))
    wheels = list(tmp_path.glob("*.whl"))
    assert len(wheels) == 1

    run(
        uv,
        "run",
        "--no-project",
        "--isolated",
        "--with",
        str(wheels[0]),
        "python",
        "-c",
        "; ".join(f"import {module}" for module in SMOKE_IMPORTS),
    )
