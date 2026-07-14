"""Spec loading: provenance digest, pinning, fetch limits (P1-10)."""

from __future__ import annotations

import hashlib
import io
from pathlib import Path

import pytest

from api_generator import loader

DOCUMENT = b"openapi: 3.0.0\ninfo: {title: t, version: '1'}\n"
DIGEST = hashlib.sha256(DOCUMENT).hexdigest()


def write_spec(tmp_path: Path) -> Path:
    path = tmp_path / "api.yaml"
    path.write_bytes(DOCUMENT)
    return path


def test_load_spec_records_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CONTREE_SPEC_SHA256", raising=False)
    spec = loader.load_spec(write_spec(tmp_path))
    assert spec.sha256 == DIGEST
    assert spec.text == DOCUMENT.decode()


def test_load_spec_verifies_pinned_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = write_spec(tmp_path)
    monkeypatch.setenv("CONTREE_SPEC_SHA256", DIGEST.upper())
    # matching pin (case-insensitive) passes
    assert loader.load_spec(path).sha256 == DIGEST

    monkeypatch.setenv("CONTREE_SPEC_SHA256", "f" * 64)
    with pytest.raises(ValueError, match="digest mismatch"):
        loader.load_spec(path)


def test_fetch_url_enforces_size_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeResponse(io.BytesIO):
        def __enter__(self) -> FakeResponse:
            return self

        def __exit__(self, *args: object) -> None:
            pass

    huge = FakeResponse(b"x" * (loader.MAX_SPEC_BYTES + 10))

    def fake_urlopen(url: str, timeout: float) -> FakeResponse:
        assert timeout == loader.FETCH_TIMEOUT
        return huge

    monkeypatch.setattr(loader.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(ValueError, match="exceeds"):
        loader.fetch_url("https://spec.example/api.yaml")


def test_fetch_url_wraps_network_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def failing_urlopen(url: str, timeout: float) -> None:
        raise OSError("connection refused")

    monkeypatch.setattr(loader.urllib.request, "urlopen", failing_urlopen)
    with pytest.raises(OSError, match=r"spec\.example"):
        loader.fetch_url("https://spec.example/api.yaml")
