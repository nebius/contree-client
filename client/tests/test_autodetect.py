from __future__ import annotations

import importlib
from types import ModuleType

import pytest

from tests.conftest import PROJECT, TOKEN
from tests.stub_server import StubServer


def test_sync_autodetect_priority(generated_package: ModuleType) -> None:
    sync = importlib.import_module("contree_client.sync")
    requests_module = importlib.import_module("contree_client.requests")
    # every backend is installed in the dev environment -> the most
    # popular one wins
    assert sync.BACKEND == "requests"
    assert sync.ContreeClient is requests_module.ContreeClient


def test_async_autodetect_priority(generated_package: ModuleType) -> None:
    asyncio_module = importlib.import_module("contree_client.asyncio")
    aiohttp_module = importlib.import_module("contree_client.aiohttp")
    assert asyncio_module.BACKEND == "aiohttp"
    assert asyncio_module.ContreeAsyncClient is aiohttp_module.ContreeAsyncClient


def test_sync_autodetect_client_works(
    generated_package: ModuleType,
    stub_server: StubServer,
) -> None:
    sync = importlib.import_module("contree_client.sync")
    with sync.ContreeClient(
        TOKEN,
        base_url=stub_server.base_url,
        project=PROJECT,
    ) as client:
        assert client.whoami().permissions["spawn"] is True


def test_sync_autodetect_falls_back_to_stdlib(
    generated_package: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sync = importlib.import_module("contree_client.sync")
    http_module = importlib.import_module("contree_client.http")
    monkeypatch.setattr(sync, "BACKEND_PRIORITY", ("nope", "http"))
    backend, client_class = sync.detect_backend()
    assert backend == "http"
    assert client_class is http_module.ContreeClient


def test_autodetect_failure_suggests_install(
    generated_package: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for module_name in ("contree_client.sync", "contree_client.asyncio"):
        module = importlib.import_module(module_name)
        monkeypatch.setattr(module, "BACKEND_PRIORITY", ("nonexistent",))
        with pytest.raises(ImportError, match="pip install contree-client"):
            module.detect_backend()


def test_autodetect_propagates_internal_import_errors(
    generated_package: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P3-02: only the backend's own missing dependency is skipped; a
    broken installed adapter must surface, not fall through."""
    sync = importlib.import_module("contree_client.sync")

    def broken_import(name: str) -> ModuleType:
        raise ModuleNotFoundError("No module named 'shutil'", name="shutil")

    monkeypatch.setattr(sync, "import_module", broken_import)
    monkeypatch.setattr(sync, "BACKEND_PRIORITY", ("requests",))
    with pytest.raises(ModuleNotFoundError, match="shutil"):
        sync.detect_backend()

    def missing_backend(name: str) -> ModuleType:
        raise ModuleNotFoundError("No module named 'requests'", name="requests")

    monkeypatch.setattr(sync, "import_module", missing_backend)
    # the expected missing dependency is skipped -> clean ImportError
    with pytest.raises(ImportError, match="pip install contree-client"):
        sync.detect_backend()
