"""Transport-level errors are catchable both as Contree exceptions and
as each backend's own native exception type.

Every backend wraps its connection/timeout errors in a class that
inherits from both `contree_client.ContreeConnectionError` (or
`ContreeTimeoutError`) and the backend's native error class (e.g.
`aiohttp.ClientConnectionError`). Code written against either the new
hierarchy or the transport library directly keeps working unchanged.
"""

from __future__ import annotations

import asyncio
import importlib
from types import ModuleType
from typing import ClassVar

import aiohttp
import httpx
import pytest
import requests
import urllib3

from tests import stub_server as stub
from tests.conftest import BACKENDS, PROJECT, TOKEN, client_class, make_invoke
from tests.stub_server import StubServer

# Per backend: the module that defines the hybrid classes, their
# names, and the native exception type they must also be an instance
# of. httpx and httpx_async share the same module and classes.
BACKEND_ERRORS = {
    "http": {
        "module": "contree_client.http",
        "connection_cls": "ContreeHttpConnectionError",
        "connection_native": OSError,
        "timeout_cls": "ContreeHttpTimeoutError",
        "timeout_native": TimeoutError,
    },
    "urllib3": {
        "module": "contree_client.urllib3",
        "connection_cls": "ContreeUrllib3ConnectionError",
        "connection_native": urllib3.exceptions.HTTPError,
        "timeout_cls": "ContreeUrllib3TimeoutError",
        "timeout_native": urllib3.exceptions.TimeoutError,
    },
    "requests": {
        "module": "contree_client.requests",
        "connection_cls": "ContreeRequestsConnectionError",
        "connection_native": requests.ConnectionError,
        "timeout_cls": "ContreeRequestsTimeoutError",
        "timeout_native": requests.Timeout,
    },
    "httpx": {
        "module": "contree_client.httpx",
        "connection_cls": "ContreeHttpxConnectionError",
        "connection_native": httpx.TransportError,
        "timeout_cls": "ContreeHttpxTimeoutError",
        "timeout_native": httpx.TimeoutException,
    },
    "httpx_async": {
        "module": "contree_client.httpx",
        "connection_cls": "ContreeHttpxConnectionError",
        "connection_native": httpx.TransportError,
        "timeout_cls": "ContreeHttpxTimeoutError",
        "timeout_native": httpx.TimeoutException,
    },
    "aiohttp": {
        "module": "contree_client.aiohttp",
        "connection_cls": "ContreeAiohttpConnectionError",
        "connection_native": aiohttp.ClientConnectionError,
        "timeout_cls": "ContreeAiohttpTimeoutError",
        "timeout_native": TimeoutError,
    },
}

# nothing listens here; the OS refuses the connection immediately
REFUSED_BASE_URL = "http://127.0.0.1:1"


@pytest.mark.parametrize("backend", BACKENDS)
def test_connection_failure_is_catchable_both_ways(
    backend: str,
    generated_package: ModuleType,
) -> None:
    exceptions = importlib.import_module("contree_client.exceptions")
    info = BACKEND_ERRORS[backend]
    module = importlib.import_module(info["module"])
    invoke = make_invoke(
        backend,
        lambda: client_class(backend)(
            TOKEN, base_url=REFUSED_BASE_URL, project=PROJECT
        ),
    )

    with pytest.raises(getattr(module, info["connection_cls"])) as excinfo:
        invoke("whoami")

    # catchable the new way...
    assert isinstance(excinfo.value, exceptions.ContreeConnectionError)
    assert isinstance(excinfo.value, exceptions.ContreeTransportError)
    assert isinstance(excinfo.value, exceptions.ContreeError)
    # ...and the old way, for code that already catches the native type
    assert isinstance(excinfo.value, info["connection_native"])
    # .args is replayed from the original exception, not collapsed to
    # a message string - __cause__ is that same original exception
    assert excinfo.value.args == excinfo.value.__cause__.args


# requests itself wraps a stalled read as ConnectionError, not Timeout
STREAM_STALL_BUCKET = dict.fromkeys(BACKENDS, "timeout") | {"requests": "connection"}


@pytest.mark.parametrize("backend", BACKENDS)
def test_read_timeout_is_catchable_both_ways(
    backend: str,
    generated_package: ModuleType,
    stub_server: StubServer,
) -> None:
    exceptions = importlib.import_module("contree_client.exceptions")
    info = BACKEND_ERRORS[backend]
    module = importlib.import_module(info["module"])
    bucket = STREAM_STALL_BUCKET[backend]
    invoke = make_invoke(
        backend,
        lambda: client_class(backend)(
            TOKEN, base_url=stub_server.base_url, project=PROJECT, timeout=1.0
        ),
    )

    with pytest.raises(getattr(module, info[f"{bucket}_cls"])) as excinfo:
        invoke(
            "inspect_image_archive",
            stub.SLOW_IMAGE_UUID,
            "/etc",
            collect=True,
        )

    # catchable the new way...
    contree_base = getattr(exceptions, f"Contree{bucket.capitalize()}Error")
    assert isinstance(excinfo.value, contree_base)
    assert isinstance(excinfo.value, exceptions.ContreeTransportError)
    assert isinstance(excinfo.value, exceptions.ContreeError)
    # ...and the old way, for code that already catches the native type
    assert isinstance(excinfo.value, info[f"{bucket}_native"])
    assert excinfo.value.args == excinfo.value.__cause__.args


def test_aiohttp_payload_error_is_catchable_both_ways(
    generated_package: ModuleType,
) -> None:
    """`aiohttp.ClientPayloadError` gets its own bucket: it means the
    body arrived but was malformed, not that the connection failed.

    The session is faked (rather than hitting the stub server) so the
    body read deterministically raises `ClientPayloadError` - this
    exercises the real `request()` wrapping, not a substitute for it.
    """
    exceptions = importlib.import_module("contree_client.exceptions")
    module = importlib.import_module("contree_client.aiohttp")
    runtime = importlib.import_module("contree_client.runtime")

    class FakeResponse:
        status = 200
        headers: ClassVar[dict[str, str]] = {}

        async def read(self) -> bytes:
            raise aiohttp.ClientPayloadError("incomplete chunked response")

    class FakeRequestCM:
        async def __aenter__(self) -> FakeResponse:
            return FakeResponse()

        async def __aexit__(self, *exc_info: object) -> None:
            return None

    class FakeSession:
        def request(self, *args: object, **kwargs: object) -> FakeRequestCM:
            return FakeRequestCM()

    async def scenario() -> BaseException:
        client = module.ContreeAsyncClient(TOKEN, base_url="http://127.0.0.1")
        client._get_session = FakeSession  # type: ignore[method-assign]
        spec = runtime.RequestSpec(method="GET", path="/x")
        with pytest.raises(module.ContreeAiohttpStreamError) as excinfo:
            await client.request(spec)
        return excinfo.value

    error = asyncio.run(scenario())
    assert isinstance(error, exceptions.ContreeStreamError)
    assert isinstance(error, exceptions.ContreeTransportError)
    assert isinstance(error, aiohttp.ClientPayloadError)


def test_httpx_wrap_preserves_request_attribute(generated_package: ModuleType) -> None:
    """`httpx.HTTPError.request` is set outside `__init__`; a naive
    rebuild from `.args` alone would leave it unset."""
    module = importlib.import_module("contree_client.httpx")
    request = httpx.Request("GET", "http://example.test")
    original = httpx.ConnectError("boom")
    original.request = request

    wrapped = module.ContreeHttpxConnectionError.wrap(original)

    assert wrapped.request is request


def test_requests_wrap_preserves_response_and_request_attributes(
    generated_package: ModuleType,
) -> None:
    module = importlib.import_module("contree_client.requests")
    request = requests.PreparedRequest()
    response = requests.Response()
    original = requests.ConnectionError("boom", request=request, response=response)

    wrapped = module.ContreeRequestsConnectionError.wrap(original)

    assert wrapped.request is request
    assert wrapped.response is response
