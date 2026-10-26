from __future__ import annotations

import json
import os
import shutil
import subprocess
import textwrap
from collections.abc import Callable
from pathlib import Path

import pytest

GO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def gorun(tmp_path: Path) -> Callable[[str], str]:
    """Run a complete Go program in an isolated module using the local client."""
    go = shutil.which("go")
    if go is None:
        pytest.fail("gorun requires Go 1.27 or later")
    (tmp_path / "go.mod").write_text(
        "module integration.example\n\ngo 1.27\n\n"
        "require github.com/nebius/contree-client/go v0.0.0\n"
        f"replace github.com/nebius/contree-client/go => {json.dumps(str(GO_ROOT))}\n",
        encoding="utf-8",
    )

    def run(source: str) -> str:
        (tmp_path / "main.go").write_text(textwrap.dedent(source), encoding="utf-8")
        result = subprocess.run(
            [go, "run", "."],
            cwd=tmp_path,
            env={**os.environ, "GOWORK": "off"},
            capture_output=True,
            text=True,
            timeout=270,
            check=False,
        )
        assert result.returncode == 0, (
            f"go run failed ({result.returncode})\n{result.stdout}\n{result.stderr}"
        )
        return result.stdout

    return run
