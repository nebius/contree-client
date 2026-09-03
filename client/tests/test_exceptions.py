"""Tests for request-level API errors."""

from __future__ import annotations

import asyncio
import importlib
from types import ModuleType
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest
import requests
import urllib3

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
def test_stream_keeps_native_connection_errors(
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

    with pytest.raises(Exception) as caught:
        invoke(
            "iter_operation_events",
            "00000000-0000-0000-0000-000000000000",
            collect=True,
        )

    assert not isinstance(caught.value, exceptions.ContreeError)


def test_aiohttp_request_maps_body_read_error(
    generated_package: ModuleType,
) -> None:
    module = importlib.import_module("contree_client.aiohttp")
    runtime = importlib.import_module("contree_client.runtime")
    exceptions = importlib.import_module("contree_client.exceptions")
    native = aiohttp.ClientPayloadError("body interrupted")
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
