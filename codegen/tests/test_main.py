from __future__ import annotations

from pathlib import Path

import pytest

from api_generator import __main__ as cli
from api_generator.__main__ import main
from api_generator.loader import load_spec
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


def test_main_dispatches_go_to_versioned_package(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    called: list[tuple[str, Path]] = []

    def generate_go(spec_source: str, package_dir: Path) -> Path:
        called.append((spec_source, package_dir))
        return package_dir

    monkeypatch.setitem(cli.GENERATORS, "go", generate_go)
    monkeypatch.setattr(
        "sys.argv",
        ["api_generator", "--spec", "synthetic.yaml", "--lang", "go"],
    )

    cli.main()

    package_dir = cli.REPO_ROOT / "go" / "v1"
    assert called == [("synthetic.yaml", package_dir)]
    assert f"generated {package_dir}" in capsys.readouterr().out


def test_cli_registers_all_language_defaults() -> None:
    assert tuple(cli.GENERATORS) == ("python", "js", "go")
    assert cli.DEFAULT_PACKAGE_DIRS == {
        "python": cli.REPO_ROOT / "client" / "contree_client",
        "js": cli.REPO_ROOT / "client-js" / "lib",
        "go": cli.REPO_ROOT / "go" / "v1",
    }


def test_cli_defaults_to_committed_spec(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CONTREE_SPEC", raising=False)
    parser = cli.Parser(config_files=())
    parser.parse_args([])
    assert Path(parser.spec) == cli.REPO_ROOT / "api/v1/spec.yaml"


@pytest.mark.parametrize("source_kind", ["file", "url", "in_place"])
def test_save_spec_preserves_input_bytes(
    source_kind: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = "# café\r\nopenapi: 3.0.0\r\npaths: {}\r\n".encode()
    source = tmp_path / "source.yaml"
    source.write_bytes(payload)
    target = source if source_kind == "in_place" else tmp_path / "api/v1/spec.yaml"
    location = str(source)
    fetched: list[str] = []
    if source_kind == "url":
        location = "https://example.invalid/spec.yaml"

        def fetch_url(url: str) -> bytes:
            fetched.append(url)
            return payload

        monkeypatch.setattr("api_generator.loader.fetch_url", fetch_url)

    def unexpected_generation(*args: object) -> Path:
        pytest.fail("saving the spec must not generate a package")

    monkeypatch.setitem(cli.GENERATORS, "python", unexpected_generation)
    monkeypatch.delenv("CONTREE_SPEC_SHA256", raising=False)
    parser = cli.Parser(config_files=())
    parser.parse_args(["--spec", location, "--save-spec", str(target)])
    parser()

    assert target.read_bytes() == payload
    assert load_spec(target).sha256 == load_spec(source).sha256
    assert fetched == ([location] if source_kind == "url" else [])
    assert list(target.parent.iterdir()) == [target]


def test_save_spec_keeps_snapshot_on_digest_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.yaml"
    source.write_text("openapi: 3.0.0\npaths: {}\n")
    target = tmp_path / "spec.yaml"
    target.write_bytes(b"previous snapshot\n")
    monkeypatch.setenv("CONTREE_SPEC_SHA256", "0" * 64)
    parser = cli.Parser(config_files=())
    parser.parse_args(["--spec", str(source), "--save-spec", str(target)])

    with pytest.raises(ValueError, match="digest mismatch"):
        parser()

    assert target.read_bytes() == b"previous snapshot\n"
