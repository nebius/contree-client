"""Behavioral tests for transport exception boundaries."""

from __future__ import annotations

import asyncio
import http.client
import importlib
import ssl
from types import ModuleType
from typing import Any
from unittest.mock import Mock

import pytest


def _spec(runtime: ModuleType) -> Any:
    return runtime.RequestSpec(method="GET", path="/x", idempotent=True)


def _assert_translation(
    error: BaseException,
    native: BaseException,
    contree_type: type[BaseException],
    native_type: type[BaseException],
) -> None:
    assert isinstance(error, contree_type)
    assert isinstance(error, native_type)
    assert error.original is native  # type: ignore[attr-defined]
    assert error.__cause__ is native
    assert str(error) == str(native) or not str(native)


def test_public_tree_keeps_only_broad_transport_categories(
    generated_package: ModuleType,
    exceptions: ModuleType,
) -> None:
    assert issubclass(exceptions.ContreeTransportError, exceptions.ContreeError)
    assert issubclass(
        exceptions.ContreeConnectionError, exceptions.ContreeTransportError
    )
    assert issubclass(exceptions.ContreeTimeoutError, exceptions.ContreeTransportError)
    assert issubclass(exceptions.ContreeTimeoutError, TimeoutError)
    assert issubclass(exceptions.ContreeStreamError, exceptions.ContreeTransportError)
    assert issubclass(exceptions.ContreeHTTPError, exceptions.ContreeTransportError)
    assert issubclass(exceptions.DecompressionError, exceptions.ContreeStreamError)
    assert generated_package.ContreeStreamError is exceptions.ContreeStreamError
    for removed in (
        "ContreeSSLError",
        "ContreeConnectionClosedError",
        "ContreeProtocolError",
    ):
        assert not hasattr(exceptions, removed)
        assert not hasattr(generated_package, removed)


def test_original_mirrors_native_cause(exceptions: ModuleType) -> None:
    native = ConnectionError("refused")
    wrapped = exceptions.ContreeConnectionError.wrap(native)
    assert wrapped.original is native
    assert wrapped.__cause__ is native
    assert str(wrapped) == "refused"


def test_api_error_metadata_is_unchanged(
    runtime: ModuleType,
    exceptions: ModuleType,
) -> None:
    error = runtime.error_for_response(
        runtime.ResponseData(
            status=410,
            headers={"retry-after": "5"},
            body=b'{"error":"gone","traceback":["frame"]}',
        )
    )
    assert isinstance(error, exceptions.GoneError)
    assert error.status == 410
    assert error.error == "gone"
    assert error.traceback == ["frame"]
    assert error.retry_after == 5


@pytest.mark.parametrize(
    ("kind", "expected_attempts"),
    [
        ("connection", 2),
        ("timeout", 2),
        ("tls", 1),
        ("invalid", 1),
        ("invalid_header", 1),
    ],
)
def test_http_request_boundary(
    kind: str,
    expected_attempts: int,
    runtime: ModuleType,
    exceptions: ModuleType,
) -> None:
    module = importlib.import_module("contree_client.http")
    native: BaseException
    if kind == "connection":
        native = ConnectionRefusedError("refused")
    elif kind == "timeout":
        native = TimeoutError("timed out")
    elif kind == "tls":
        native = ssl.SSLError("bad certificate")
    elif kind == "invalid_header":
        native = ValueError("invalid header")
    else:
        native = http.client.InvalidURL("bad URL")

    class Connection:
        sock = None
        timeout = None
        calls = 0

        def request(self, *args: object, **kwargs: object) -> None:
            self.calls += 1
            raise native

        def getresponse(self) -> None:
            raise AssertionError("request must fail first")

        def close(self) -> None:
            pass

    connection = Connection()

    class Pool:
        def acquire(self, deadline: float | None) -> tuple[Connection, bool]:
            return connection, False

        def discard(self, unused: Connection) -> None:
            pass

        def close(self) -> None:
            pass

    client = module.ContreeClient(
        "token",
        base_url="http://example.test",
        retry=runtime.RetryPolicy(delays=(0.0,), max_attempts=2),
    )
    client._pool = Pool()
    with pytest.raises(BaseException) as caught:
        client.call(_spec(runtime))

    assert connection.calls == expected_attempts
    if kind in ("invalid", "invalid_header"):
        assert caught.value is native
    elif kind == "timeout":
        _assert_translation(
            caught.value, native, exceptions.ContreeTimeoutError, TimeoutError
        )
    else:
        _assert_translation(
            caught.value, native, exceptions.ContreeConnectionError, OSError
        )


@pytest.mark.parametrize(
    ("kind", "expected_attempts"),
    [
        ("connection", 2),
        ("timeout", 2),
        ("tls", 1),
        ("invalid", 1),
        ("invalid_header", 1),
        ("decode", 2),
    ],
)
def test_urllib3_request_boundary(
    kind: str,
    expected_attempts: int,
    runtime: ModuleType,
    exceptions: ModuleType,
) -> None:
    urllib3 = pytest.importorskip("urllib3")
    module = importlib.import_module("contree_client.urllib3")
    if kind == "connection":
        native = urllib3.exceptions.NewConnectionError(None, "refused")
    elif kind == "timeout":
        native = urllib3.exceptions.ConnectTimeoutError("timed out")
    elif kind == "tls":
        native = urllib3.exceptions.SSLError("bad certificate")
    elif kind == "invalid":
        native = urllib3.exceptions.LocationParseError("bad URL")
    elif kind == "invalid_header":
        native = urllib3.exceptions.InvalidHeader("invalid header")
    else:
        native = urllib3.exceptions.DecodeError("bad gzip")

    class Pool:
        calls = 0

        def request(self, *args: object, **kwargs: object) -> None:
            self.calls += 1
            raise native

    pool = Pool()
    client = module.ContreeClient(
        "token",
        base_url="http://example.test",
        retry=runtime.RetryPolicy(delays=(0.0,), max_attempts=2),
        urllib3_pool_manager=pool,
    )
    with pytest.raises(BaseException) as caught:
        client.call(_spec(runtime))

    assert pool.calls == expected_attempts
    if kind in ("invalid", "invalid_header"):
        assert caught.value is native
    elif kind == "timeout":
        _assert_translation(
            caught.value,
            native,
            exceptions.ContreeTimeoutError,
            urllib3.exceptions.TimeoutError,
        )
    elif kind == "decode":
        assert isinstance(caught.value, exceptions.DecompressionError)
        assert caught.value.original is native
    else:
        _assert_translation(
            caught.value,
            native,
            exceptions.ContreeConnectionError,
            urllib3.exceptions.HTTPError,
        )
        if kind == "connection":
            assert not isinstance(caught.value, exceptions.ContreeTimeoutError)


@pytest.mark.parametrize(
    ("kind", "expected_attempts"),
    [
        ("connection", 2),
        ("timeout", 2),
        ("tls", 1),
        ("invalid", 1),
        ("invalid_header", 1),
        ("stream", 2),
        ("decode", 1),
    ],
)
def test_requests_request_boundary(
    kind: str,
    expected_attempts: int,
    runtime: ModuleType,
    exceptions: ModuleType,
) -> None:
    requests = pytest.importorskip("requests")
    module = importlib.import_module("contree_client.requests")
    native_types = {
        "connection": requests.ConnectionError,
        "timeout": requests.ConnectTimeout,
        "tls": requests.exceptions.SSLError,
        "invalid": requests.exceptions.InvalidURL,
        "invalid_header": requests.exceptions.InvalidHeader,
        "stream": requests.exceptions.ChunkedEncodingError,
        "decode": requests.exceptions.ContentDecodingError,
    }
    native = native_types[kind](kind)

    class Session:
        calls = 0

        def request(self, *args: object, **kwargs: object) -> None:
            self.calls += 1
            raise native

    session = Session()
    client = module.ContreeClient(
        "token",
        base_url="http://example.test",
        retry=runtime.RetryPolicy(delays=(0.0,), max_attempts=2),
        requests_session=session,
    )
    with pytest.raises(BaseException) as caught:
        client.call(_spec(runtime))

    assert session.calls == expected_attempts
    if kind in ("invalid", "invalid_header"):
        assert caught.value is native
    elif kind == "timeout":
        _assert_translation(
            caught.value,
            native,
            exceptions.ContreeTimeoutError,
            requests.Timeout,
        )
    elif kind == "stream":
        assert isinstance(caught.value, exceptions.ContreeStreamError)
        assert caught.value.original is native
    elif kind == "decode":
        assert isinstance(caught.value, exceptions.DecompressionError)
        assert caught.value.original is native
    else:
        _assert_translation(
            caught.value,
            native,
            exceptions.ContreeConnectionError,
            requests.ConnectionError,
        )


@pytest.mark.parametrize(
    ("kind", "expected_attempts"),
    [
        ("connection", 2),
        ("timeout", 2),
        ("tls", 1),
        ("remote_protocol", 2),
        ("invalid_header", 1),
        ("unsupported", 1),
        ("decode", 1),
    ],
)
def test_httpx_request_boundary(
    kind: str,
    expected_attempts: int,
    runtime: ModuleType,
    exceptions: ModuleType,
) -> None:
    httpx = pytest.importorskip("httpx")
    module = importlib.import_module("contree_client.httpx")
    native_types = {
        "connection": httpx.ConnectError,
        "timeout": httpx.ConnectTimeout,
        "tls": httpx.ConnectError,
        "remote_protocol": httpx.RemoteProtocolError,
        "invalid_header": httpx.LocalProtocolError,
        "unsupported": httpx.UnsupportedProtocol,
        "decode": httpx.DecodingError,
    }
    native = native_types[kind](kind)
    if kind == "tls":
        native.__cause__ = ssl.SSLError("bad certificate")

    class Backend:
        calls = 0

        def request(self, *args: object, **kwargs: object) -> None:
            self.calls += 1
            raise native

    backend = Backend()
    client = module.ContreeClient(
        "token",
        base_url="http://example.test",
        retry=runtime.RetryPolicy(delays=(0.0,), max_attempts=2),
        httpx_client=backend,
    )
    with pytest.raises(BaseException) as caught:
        client.call(_spec(runtime))

    assert backend.calls == expected_attempts
    if kind in ("invalid_header", "unsupported"):
        assert caught.value is native
        assert not isinstance(caught.value, exceptions.ContreeConnectionError)
    elif kind == "timeout":
        _assert_translation(
            caught.value,
            native,
            exceptions.ContreeTimeoutError,
            httpx.TimeoutException,
        )
    elif kind == "decode":
        assert isinstance(caught.value, exceptions.DecompressionError)
        assert caught.value.original is native
    else:
        _assert_translation(
            caught.value,
            native,
            exceptions.ContreeConnectionError,
            httpx.TransportError,
        )
        assert not hasattr(exceptions, "ContreeConnectionClosedError")


@pytest.mark.parametrize(
    ("kind", "expected_attempts"),
    [
        ("connection", 2),
        ("timeout", 2),
        ("tls", 1),
        ("fingerprint", 1),
        ("invalid", 1),
        ("invalid_header", 1),
        ("payload", 2),
    ],
)
def test_aiohttp_request_boundary(
    kind: str,
    expected_attempts: int,
    runtime: ModuleType,
    exceptions: ModuleType,
) -> None:
    aiohttp = pytest.importorskip("aiohttp")
    module = importlib.import_module("contree_client.aiohttp")
    if kind == "connection":
        native = aiohttp.ClientConnectionError("refused")
    elif kind == "timeout":
        native = TimeoutError("timed out")
    elif kind == "tls":
        connection_key = Mock(ssl=True, host="example.test", port=443, is_ssl=True)
        native = aiohttp.ClientConnectorSSLError(
            connection_key, ssl.SSLError("bad certificate")
        )
    elif kind == "fingerprint":
        native = aiohttp.ServerFingerprintMismatch(
            b"expected", b"actual", "example.test", 443
        )
    elif kind == "invalid":
        native = aiohttp.InvalidURL("bad URL")
    elif kind == "invalid_header":
        native = ValueError("invalid header")
    else:
        native = aiohttp.ClientPayloadError("truncated")

    class RequestContext:
        async def __aenter__(self) -> None:
            raise native

        async def __aexit__(self, *args: object) -> None:
            pass

    class Session:
        calls = 0
        closed = False

        def request(self, *args: object, **kwargs: object) -> RequestContext:
            self.calls += 1
            return RequestContext()

    session = Session()

    async def scenario() -> BaseException:
        client = module.ContreeAsyncClient(
            "token",
            base_url="http://example.test",
            retry=runtime.RetryPolicy(delays=(0.0,), max_attempts=2),
            aiohttp_session=session,
        )
        with pytest.raises(BaseException) as caught:
            await client.call(_spec(runtime))
        return caught.value

    error = asyncio.run(scenario())
    assert session.calls == expected_attempts
    if kind in ("invalid", "invalid_header"):
        assert error is native
    elif kind == "timeout":
        _assert_translation(error, native, exceptions.ContreeTimeoutError, TimeoutError)
    elif kind == "payload":
        _assert_translation(
            error,
            native,
            exceptions.ContreeStreamError,
            aiohttp.ClientPayloadError,
        )
    else:
        _assert_translation(
            error,
            native,
            exceptions.ContreeConnectionError,
            aiohttp.ClientConnectionError,
        )


@pytest.mark.parametrize(
    ("module_name", "class_name"),
    [
        ("http", "ContreeClient"),
        ("urllib3", "ContreeClient"),
        ("requests", "ContreeClient"),
        ("httpx", "ContreeClient"),
        ("aiohttp", "ContreeAsyncClient"),
    ],
)
def test_unsupported_scheme_fails_before_transport(
    module_name: str,
    class_name: str,
    generated_package: ModuleType,
) -> None:
    module = importlib.import_module(f"contree_client.{module_name}")
    with pytest.raises(ValueError, match="unsupported base_url scheme"):
        getattr(module, class_name)("token", base_url="ftp://example.test")
