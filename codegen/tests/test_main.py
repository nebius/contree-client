from __future__ import annotations

from pathlib import Path

import pytest

from api_generator.__main__ import main
from api_generator.python import emitter
from api_generator.python.emitter import GENERATED_FILES


def test_main_generates_package(
    spec_source: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    package_dir = tmp_path / "contree_client"
    monkeypatch.setattr(
        "sys.argv",
        ["api_generator", "--spec", spec_source, "--package", str(package_dir)],
    )
    main()
    for name in GENERATED_FILES:
        assert (package_dir / name).is_file()
    assert str(package_dir) in capsys.readouterr().out


def test_generate_is_transactional(
    spec_source: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P2-13: a failure mid-generation must not touch the previously
    working package."""
    package_dir = tmp_path / "contree_client"
    package_dir.mkdir()
    sentinel = "# the previous, working generation\n"
    for name in emitter.GENERATED_FILES:
        (package_dir / name).write_text(sentinel)

    def failing_ruff(paths: list[Path]) -> None:
        raise RuntimeError("ruff exploded")

    monkeypatch.setattr(emitter, "run_ruff", failing_ruff)
    with pytest.raises(RuntimeError, match="ruff exploded"):
        emitter.generate(spec_source, package_dir)

    for name in emitter.GENERATED_FILES:
        assert (package_dir / name).read_text() == sentinel
    # and no staging leftovers next to the package
    assert not list(tmp_path.glob(".generate-*"))
