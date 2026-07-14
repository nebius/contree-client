from __future__ import annotations

import asyncio
import importlib
import os
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

# the directory that contains both contree_client/ and tests/ - the
# repository's client/ dir or the root of an unpacked sdist
CLIENT_ROOT = Path(__file__).resolve().parent.parent

if str(CLIENT_ROOT) not in sys.path:
    sys.path.insert(0, str(CLIENT_ROOT))

try:  # the distribution sdist ships no generator: the package is prebuilt
    from api_generator.loader import load_spec
    from api_generator.python.emitter import generate

    HAVE_GENERATOR = True
except ImportError:
    HAVE_GENERATOR = False

# generator-only test modules are not collectable without codegen
collect_ignore = (
    []
    if HAVE_GENERATOR
    else [
        "test_ir.py",
        "test_js_reference.py",
        "test_loader.py",
        "test_main.py",
        "test_naming.py",
    ]
)

from tests.stub_server import StubServer  # noqa: E402

TOKEN = "test-token"
PROJECT = "test-project"

SYNC_BACKENDS = ("http", "urllib3", "requests", "httpx")
ASYNC_BACKENDS = ("httpx_async", "aiohttp")
BACKENDS = SYNC_BACKENDS + ASYNC_BACKENDS


@pytest.fixture(scope="session")
def spec_source(tmp_path_factory: pytest.TempPathFactory) -> str:
    """The OpenAPI spec location as a local path.

    The spec is never stored in the repository: the CONTREE_SPEC
    environment variable (a CI secret) points at it. Tests that need
    the spec are skipped when the variable is not exported; a URL is
    fetched once per session so every consumer works from the same
    local file.
    """
    source = os.environ.get("CONTREE_SPEC", "")
    if not source:
        pytest.skip("the CONTREE_SPEC environment variable is not set")
    if source.startswith(("http://", "https://")):
        path = tmp_path_factory.mktemp("spec") / "api.yaml"
        path.write_text(load_spec(source).text, encoding="utf-8")
        return str(path)
    return source


@pytest.fixture(scope="session")
def generated_package(request: pytest.FixtureRequest) -> ModuleType:
    """Import the package, regenerating it first when possible.

    Three layouts work: the dev repo with CONTREE_SPEC exported
    (regenerate from the fresh spec), a checkout with pregenerated
    sources (a CI artifact built once on one job), and the
    distribution sdist where the modules are baked in and no
    generator ships at all.
    """
    if HAVE_GENERATOR and os.environ.get("CONTREE_SPEC"):
        generate(
            request.getfixturevalue("spec_source"),
            CLIENT_ROOT / "contree_client",
        )
    else:
        # no spec: the package must already be complete - pregenerated
        # sources, the sdist, or an installed wheel (CI removes the
        # source tree and installs the built artifact instead)
        try:
            importlib.import_module("contree_client.base")
        except ImportError:
            pytest.skip(
                "the generated contree_client package is not available;"
                " export CONTREE_SPEC or install the built wheel"
            )
    return importlib.import_module("contree_client")


@pytest.fixture(scope="session")
def models(generated_package: ModuleType) -> ModuleType:
    return importlib.import_module("contree_client.models")


@pytest.fixture(scope="session")
def runtime(generated_package: ModuleType) -> ModuleType:
    return importlib.import_module("contree_client.runtime")


@pytest.fixture(scope="session")
def operations(generated_package: ModuleType) -> ModuleType:
    return importlib.import_module("contree_client.operations")


@pytest.fixture(scope="session")
def exceptions(generated_package: ModuleType) -> ModuleType:
    return importlib.import_module("contree_client.exceptions")


@pytest.fixture(scope="session")
def profiles(generated_package: ModuleType) -> ModuleType:
    return importlib.import_module("contree_client.profiles")


@pytest.fixture(scope="session")
def stub_server_session() -> Any:
    server = StubServer()
    server.start()
    yield server
    server.stop()


@pytest.fixture
def stub_server(stub_server_session: StubServer) -> StubServer:
    stub_server_session.captured.clear()
    stub_server_session.attempts.clear()
    stub_server_session.connection_count = 0
    return stub_server_session


def client_class(backend: str) -> Any:
    module_name = "httpx" if backend == "httpx_async" else backend
    module = importlib.import_module(f"contree_client.{module_name}")
    if backend in ASYNC_BACKENDS:
        return module.ContreeAsyncClient
    return module.ContreeClient


def make_client(backend: str, base_url: str) -> Any:
    return client_class(backend)(TOKEN, base_url=base_url, project=PROJECT)


def make_invoke(
    backend: str,
    client_factory: Callable[[], Any],
) -> Callable[..., Any]:
    """Build a uniform sync/async caller for one backend.

    The returned callable runs `call(method_name, *args, **kwargs)`;
    `collect=True` materializes (async) iterators into a list and
    `take=N` consumes N items and closes the iterator early - both
    before the client is closed.
    """

    def call(method_name: str, *args: Any, **kwargs: Any) -> Any:
        collect = kwargs.pop("collect", False)
        take = kwargs.pop("take", None)
        if backend in ASYNC_BACKENDS:

            async def run() -> Any:
                client = client_factory()
                try:
                    result = getattr(client, method_name)(*args, **kwargs)
                    if take is not None:
                        items = []
                        async for item in result:
                            items.append(item)
                            if len(items) >= take:
                                break
                        await result.aclose()
                        return items
                    if collect:
                        return [item async for item in result]
                    return await result
                finally:
                    await client.close()

            return asyncio.run(run())

        client = client_factory()
        try:
            result = getattr(client, method_name)(*args, **kwargs)
            if take is not None:
                items = []
                for item in result:
                    items.append(item)
                    if len(items) >= take:
                        break
                result.close()
                return items
            if collect:
                return list(result)
            return result
        finally:
            client.close()

    return call


@pytest.fixture(params=BACKENDS)
def invoke(
    request: pytest.FixtureRequest,
    generated_package: ModuleType,
    stub_server: StubServer,
) -> Callable[..., Any]:
    """Call a client method uniformly across all backends (stub server)."""
    backend: str = request.param
    base_url = stub_server.base_url
    return make_invoke(backend, lambda: make_client(backend, base_url))
