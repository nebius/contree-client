"""Shared error contracts for buffered and streaming requests."""

from __future__ import annotations

import asyncio
import http.client
import importlib
from types import ModuleType
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import httpx
import pytest
import requests
import urllib3
from aiohttp.http_exceptions import TransferEncodingError

from tests import stub_server as server
from tests.conftest import BACKENDS, TOKEN, client_class, make_invoke

STATUS_ERRORS = (
    (400, "BadRequestError"),
    (401, "AuthenticationError"),
    (403, "PermissionDeniedError"),
    (404, "NotFoundError"),
    (409, "ConflictError"),
    (410, "GoneError"),
    (418, "APIStatusError"),
    (422, "UnprocessableEntityError"),
    (425, "TooEarlyError"),
    (429, "RateLimitError"),
    (500, "ServerError"),
    (599, "ServerError"),
)


def test_public_error_hierarchy(exceptions: ModuleType) -> None:
    assert issubclass(exceptions.APIConnectionError, exceptions.ContreeError)
    assert issubclass(exceptions.APIStatusError, exceptions.ContreeError)
    for _, name in STATUS_ERRORS:
        assert issubclass(getattr(exceptions, name), exceptions.APIStatusError)

    assert exceptions.APIConnectionError("offline").timed_out is False
    assert exceptions.APIConnectionError("timeout", timed_out=True).timed_out is True


@pytest.mark.parametrize(("status_code", "name"), STATUS_ERRORS)
def test_error_for_response_mapping(
    runtime: ModuleType,
    exceptions: ModuleType,
    status_code: int,
    name: str,
) -> None:
    error = runtime.error_for_response(
        status_code,
        {"retry-after": "7"},
        b'{"error":"failed","traceback":["line"]}',
    )

    assert type(error) is getattr(exceptions, name)
    assert error.status == status_code
    assert error.error == "failed"
    assert error.traceback == ["line"]
    assert error.retry_after == 7


@pytest.mark.parametrize("backend", BACKENDS)
def test_request_maps_connection_errors(
    backend: str,
    generated_package: ModuleType,
) -> None:
    exceptions = importlib.import_module("contree_client.exceptions")
    invoke = make_invoke(
        backend,
        lambda: client_class(backend)(
            TOKEN,
            base_url="http://127.0.0.1:1",
        ),
    )

    with pytest.raises(exceptions.APIConnectionError) as caught:
        invoke("whoami")

    assert type(caught.value) is exceptions.APIConnectionError
    assert isinstance(caught.value.__cause__, Exception)
    assert not isinstance(caught.value.__cause__, exceptions.ContreeError)


@pytest.mark.parametrize("backend", BACKENDS)
def test_stream_maps_connection_errors(
    backend: str,
    generated_package: ModuleType,
) -> None:
    exceptions = importlib.import_module("contree_client.exceptions")
    invoke = make_invoke(
        backend,
        lambda: client_class(backend)(
            TOKEN,
            base_url="http://127.0.0.1:1",
        ),
    )

    with pytest.raises(exceptions.APIConnectionError) as caught:
        invoke(
            "iter_operation_events",
            "00000000-0000-0000-0000-000000000000",
            collect=True,
        )

    assert isinstance(caught.value.__cause__, Exception)
    assert not isinstance(caught.value.__cause__, exceptions.ContreeError)


def test_aiohttp_request_maps_body_read_error(
    generated_package: ModuleType,
) -> None:
    module = importlib.import_module("contree_client.aiohttp")
    runtime = importlib.import_module("contree_client.runtime")
    exceptions = importlib.import_module("contree_client.exceptions")
    native = aiohttp.ClientPayloadError("body interrupted")
    native.__cause__ = TransferEncodingError("incomplete chunk")
    response = MagicMock(status=200, headers={})
    response.read = AsyncMock(side_effect=native)
    request = MagicMock()
    request.__aenter__.return_value = response
    session = MagicMock()
    session.request.return_value = request

    async def run() -> None:
        client = module.ContreeAsyncClient(
            TOKEN,
            base_url="http://127.0.0.1",
            aiohttp_session=session,
        )
        with pytest.raises(exceptions.APIConnectionError) as caught:
            await client.request(runtime.RequestSpec(method="GET", path="/x"))
        assert caught.value.__cause__ is native

    asyncio.run(run())


def test_requests_maps_nonstandard_error_status(
    generated_package: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = importlib.import_module("contree_client.requests")
    runtime = importlib.import_module("contree_client.runtime")
    exceptions = importlib.import_module("contree_client.exceptions")
    response = requests.Response()
    response.status_code = 600
    response._content = b'{"error":"failed"}'

    session = requests.Session()
    monkeypatch.setattr(session, "request", MagicMock(return_value=response))
    client = module.ContreeClient(
        TOKEN,
        base_url="http://127.0.0.1",
        requests_session=session,
    )

    with pytest.raises(exceptions.ServerError) as caught:
        client.request(runtime.RequestSpec(method="GET", path="/x"))

    assert caught.value.status == 600


def test_requests_marks_wrapped_read_timeout(
    generated_package: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = importlib.import_module("contree_client.requests")
    runtime = importlib.import_module("contree_client.runtime")
    exceptions = importlib.import_module("contree_client.exceptions")
    timeout = urllib3.exceptions.ReadTimeoutError(None, "/x", "read timed out")
    native = requests.exceptions.ConnectionError(timeout)

    session = requests.Session()
    monkeypatch.setattr(session, "request", MagicMock(side_effect=native))
    client = module.ContreeClient(
        TOKEN,
        base_url="http://127.0.0.1",
        requests_session=session,
    )

    with pytest.raises(exceptions.APIConnectionError) as caught:
        client.request(runtime.RequestSpec(method="GET", path="/x"))

    assert caught.value.timed_out is True
    assert caught.value.__cause__ is native


def test_urllib3_new_connection_error_is_not_a_deadline_timeout(
    generated_package: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = importlib.import_module("contree_client.urllib3")
    base = importlib.import_module("contree_client.base")
    runtime = importlib.import_module("contree_client.runtime")
    exceptions = importlib.import_module("contree_client.exceptions")
    clock = MagicMock()
    clock.monotonic.return_value = 100.0
    monkeypatch.setattr(base, "time", clock)
    monkeypatch.setattr(runtime, "monotonic", clock.monotonic)

    native = urllib3.exceptions.NewConnectionError(
        MagicMock(),
        "connection refused",
    )
    pool = MagicMock()
    pool.request.side_effect = native
    client = module.ContreeClient(
        TOKEN,
        base_url="http://127.0.0.1",
        timeout=300.0,
        urllib3_pool_manager=pool,
    )
    spec = runtime.RequestSpec(method="GET", path="/x", deadline=130.0)

    with pytest.raises(exceptions.APIConnectionError) as caught:
        client.call(spec)

    assert caught.value.timed_out is False
    assert caught.value.__cause__ is native


def native_read_timeout(backend: str) -> Exception:
    if backend == "http":
        return TimeoutError("read timed out")
    if backend == "urllib3":
        return urllib3.exceptions.ReadTimeoutError(None, "/events", "read timed out")
    if backend == "requests":
        cause = urllib3.exceptions.ReadTimeoutError(None, "/events", "read timed out")
        return requests.exceptions.ConnectionError(cause)
    if backend in ("httpx", "httpx_async"):
        return httpx.ReadTimeout("read timed out")
    if backend == "aiohttp":
        return aiohttp.SocketTimeoutError("read timed out")
    raise AssertionError(f"unknown backend {backend}")


def native_connect_timeout(backend: str) -> Exception:
    if backend == "http":
        return TimeoutError("connect timed out")
    if backend == "urllib3":
        return urllib3.exceptions.ConnectTimeoutError(None, "connect timed out")
    if backend == "requests":
        return requests.exceptions.ConnectTimeout("connect timed out")
    if backend in ("httpx", "httpx_async"):
        return httpx.ConnectTimeout("connect timed out")
    if backend == "aiohttp":
        return aiohttp.ConnectionTimeoutError("connect timed out")
    raise AssertionError(f"unknown backend {backend}")


def native_failure(backend: str, kind: str) -> BaseException:
    if kind == "read_timeout":
        return native_read_timeout(backend)
    if kind == "connect_timeout":
        return native_connect_timeout(backend)
    if kind == "unknown":
        return ValueError("invalid input")
    if kind == "cancelled":
        return asyncio.CancelledError()
    if backend == "http":
        return (
            ConnectionResetError("reset")
            if kind == "reset"
            else http.client.IncompleteRead(b"partial")
        )
    if backend == "urllib3":
        return urllib3.exceptions.ProtocolError(
            "interrupted", ConnectionResetError("reset")
        )
    if backend == "requests":
        return (
            requests.exceptions.ConnectionError("reset")
            if kind == "reset"
            else requests.exceptions.ChunkedEncodingError("truncated")
        )
    if backend in ("httpx", "httpx_async"):
        return (
            httpx.ReadError("reset")
            if kind == "reset"
            else httpx.RemoteProtocolError("truncated")
        )
    if kind == "reset":
        return aiohttp.ClientConnectionError("reset")
    error = aiohttp.ClientPayloadError("truncated")
    error.__cause__ = TransferEncodingError("incomplete chunk")
    return error


@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.parametrize("method", ["request", "stream"])
@pytest.mark.parametrize(
    "kind",
    ["connect_timeout", "read_timeout", "reset", "truncated", "unknown", "cancelled"],
)
def test_request_and_stream_share_transport_errors(
    backend: str,
    method: str,
    kind: str,
    generated_package: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    runtime: ModuleType,
    exceptions: ModuleType,
) -> None:
    native = native_failure(backend, kind)

    def factory():
        client = client_class(backend)(TOKEN, base_url="http://127.0.0.1")
        if backend == "http":
            target, name = client, "_send_on"
        elif backend == "urllib3":
            target, name = client._http, "request"
        elif backend in ("requests", "aiohttp"):
            target, name = client._session, "request"
        else:
            target, name = client._client, "send"
        mock = AsyncMock if backend == "httpx_async" else MagicMock
        monkeypatch.setattr(target, name, mock(side_effect=native))
        return client

    invoke = make_invoke(backend, factory)
    expected = (
        type(native)
        if kind in ("unknown", "cancelled")
        else exceptions.APIConnectionError
    )
    with pytest.raises(expected) as caught:
        invoke(
            method,
            runtime.RequestSpec(method="GET", path="/x"),
            collect=method == "stream",
        )
    if kind == "unknown":
        assert caught.value is native
    elif kind != "cancelled":
        assert caught.value.__cause__ is native
        assert caught.value.timed_out is (kind in ("read_timeout", "connect_timeout"))
    # asyncio.run() can replace CancelledError on Python 3.10; its type,
    # checked above, is the public cancellation contract.


@pytest.mark.parametrize("method", ["request", "stream", "raw_stream"])
@pytest.mark.parametrize(("status_code", "name"), STATUS_ERRORS)
def test_request_and_stream_share_http_errors(
    invoke,
    method: str,
    status_code: int,
    name: str,
    monkeypatch: pytest.MonkeyPatch,
    runtime: ModuleType,
    exceptions: ModuleType,
) -> None:
    def route(request, attempts):
        reply = server.json_reply(
            status_code, {"error": "failed", "traceback": ["line"]}
        )
        reply.headers["Retry-After"] = "7"
        return reply

    monkeypatch.setattr(server, "route", route)
    with pytest.raises(getattr(exceptions, name)) as caught:
        invoke(
            "stream" if method == "raw_stream" else method,
            runtime.RequestSpec(method="GET", path="/x"),
            collect=method != "request",
            **({"auto_decompress": False} if method == "raw_stream" else {}),
        )
    assert caught.value.status == status_code
    assert caught.value.error == "failed"
    assert caught.value.traceback == ["line"]
    assert caught.value.retry_after == 7
