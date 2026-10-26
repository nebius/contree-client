from __future__ import annotations

import shutil
import subprocess
import textwrap
from collections.abc import Callable
from pathlib import Path

import pytest


@pytest.fixture
def jsrun(tmp_path: Path) -> Callable[[str], str]:
    """Run an ES module from tmp_path and return its stdout."""
    node = shutil.which("node")
    if node is None:
        pytest.fail("jsrun requires Node.js 18.17 or later")

    def run(source: str) -> str:
        script = tmp_path / "main.mjs"
        script.write_text(textwrap.dedent(source), encoding="utf-8")
        result = subprocess.run(
            [node, str(script)],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            timeout=270,
            check=False,
        )
        assert result.returncode == 0, (
            f"node failed ({result.returncode})\n{result.stdout}\n{result.stderr}"
        )
        return result.stdout

    return run
