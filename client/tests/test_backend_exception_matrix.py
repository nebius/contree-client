"""Policy matrix for every exception exported by the HTTP backends."""

from __future__ import annotations

import asyncio
import errno
import http.client
import inspect
import socket
import ssl
from types import ModuleType
from typing import Any
from unittest.mock import Mock

import aiohttp
import httpx
import pytest
import requests
import urllib3

NATIVE = "native"
TRANSPORT = "transport"
TRANSPORT_RETRYABLE = "transport-retryable"
CONNECTION = "connection"
CONNECTION_RETRYABLE = "connection-retryable"
TIMEOUT_RETRYABLE = "timeout-retryable"
DECOMPRESSION = "decompression"
DECOMPRESSION_RETRYABLE = "decompression-retryable"
API_RETRYABLE = "api-retryable"


# Every public exception exported by the installed backend has one policy.
# A dependency update therefore requires an explicit decision for new classes.
BACKEND_POLICIES = {
    "http": {
        TRANSPORT: {
            "BadStatusLine",
            "CannotSendHeader",
            "CannotSendRequest",
            "HTTPException",
            "ImproperConnectionState",
            "InvalidURL",
            "LineTooLong",
            "NotConnected",
            "ResponseNotReady",
            "UnimplementedFileMode",
            "UnknownProtocol",
            "UnknownTransferEncoding",
            "error",
        },
        TRANSPORT_RETRYABLE: {"IncompleteRead"},
        CONNECTION_RETRYABLE: {"RemoteDisconnected"},
    },
    "urllib3": {
        TRANSPORT: {
            "BodyNotHttplibCompatible",
            "ClosedPoolError",
            "ConnectionError",
            "EmptyPoolError",
            "FullPoolError",
            "HTTPError",
            "HeaderParsingError",
            "HostChangedError",
            "InvalidHeader",
            "LocationParseError",
            "LocationValueError",
            "MaxRetryError",
            "PoolError",
            "ProtocolError",
            "ProxyError",
            "ProxySchemeUnknown",
            "RequestError",
            "ResponseError",
            "ResponseNotChunked",
            "TimeoutStateError",
            "URLSchemeUnknown",
            "UnrewindableBodyError",
        },
        TRANSPORT_RETRYABLE: {
            "IncompleteRead",
            "InvalidChunkLength",
        },
        CONNECTION: {
            "NameResolutionError",
            "NewConnectionError",
            "SSLError",
        },
        TIMEOUT_RETRYABLE: {
            "ConnectTimeoutError",
            "ReadTimeoutError",
            "TimeoutError",
        },
        DECOMPRESSION_RETRYABLE: {"DecodeError"},
    },
    "requests": {
        TRANSPORT: {
            "HTTPError",
            "InvalidHeader",
            "InvalidJSONError",
            "InvalidProxyURL",
            "InvalidSchema",
            "InvalidURL",
            "JSONDecodeError",
            "MissingSchema",
            "ProxyError",
            "RequestException",
            "RetryError",
            "StreamConsumedError",
            "TooManyRedirects",
            "URLRequired",
            "UnrewindableBodyError",
        },
        TRANSPORT_RETRYABLE: {"ChunkedEncodingError"},
        CONNECTION: {"ConnectionError", "SSLError"},
        TIMEOUT_RETRYABLE: {"ConnectTimeout", "ReadTimeout", "Timeout"},
        DECOMPRESSION: {"ContentDecodingError"},
    },
    "httpx": {
        NATIVE: {"CookieConflict"},
        TRANSPORT: {
            "CloseError",
            "HTTPError",
            "InvalidURL",
            "LocalProtocolError",
            "NetworkError",
            "ProtocolError",
            "ProxyError",
            "ReadError",
            "RemoteProtocolError",
            "RequestError",
            "RequestNotRead",
            "ResponseNotRead",
            "StreamClosed",
            "StreamConsumed",
            "StreamError",
            "TooManyRedirects",
            "TransportError",
            "UnsupportedProtocol",
            "WriteError",
        },
        CONNECTION: {"ConnectError"},
        TIMEOUT_RETRYABLE: {
            "ConnectTimeout",
            "PoolTimeout",
            "ReadTimeout",
            "TimeoutException",
            "WriteTimeout",
        },
        DECOMPRESSION: {"DecodingError"},
        API_RETRYABLE: {"HTTPStatusError"},
    },
    "aiohttp": {
        NATIVE: {"WSMessageTypeError"},
        TRANSPORT: {
            "ClientError",
            "ClientHttpProxyError",
            "ClientProxyConnectionError",
            "ClientResponseError",
            "ContentTypeError",
            "InvalidURL",
            "InvalidUrlClientError",
            "InvalidUrlRedirectClientError",
            "NonHttpUrlClientError",
            "NonHttpUrlRedirectClientError",
            "RedirectClientError",
            "TooManyRedirects",
            "WSServerHandshakeError",
        },
        TRANSPORT_RETRYABLE: {"ClientPayloadError"},
        CONNECTION: {
            "ClientConnectionError",
            "ClientConnectorCertificateError",
            "ClientConnectorDNSError",
            "ClientConnectorError",
            "ClientConnectorSSLError",
            "ClientOSError",
            "ClientSSLError",
            "ServerConnectionError",
            "ServerFingerprintMismatch",
            "UnixClientConnectorError",
        },
        CONNECTION_RETRYABLE: {
            "ClientConnectionResetError",
            "ServerDisconnectedError",
        },
        TIMEOUT_RETRYABLE: {
            "ConnectionTimeoutError",
            "ServerTimeoutError",
            "SocketTimeoutError",
        },
    },
}


def _backend_classes(backend: str) -> dict[str, type[BaseException]]:
    if backend == "http":
        module = http.client

        def selected(value: type[Any]) -> bool:
            return value.__module__ == module.__name__ and issubclass(
                value, http.client.HTTPException
            )

    elif backend == "urllib3":
        module = urllib3.exceptions

        def selected(value: type[Any]) -> bool:
            return value.__module__ == module.__name__ and issubclass(
                value, urllib3.exceptions.HTTPError
            )

    elif backend == "requests":
        module = requests.exceptions

        def selected(value: type[Any]) -> bool:
            return value.__module__ == module.__name__ and issubclass(
                value, requests.exceptions.RequestException
            )

    elif backend == "httpx":
        module = httpx

        def selected(value: type[Any]) -> bool:
            return value.__module__.startswith("httpx") and (
                issubclass(value, (httpx.HTTPError, httpx.StreamError))
                or value in (httpx.CookieConflict, httpx.InvalidURL)
            )

    else:
        module = aiohttp.client_exceptions

        def selected(value: type[Any]) -> bool:
            return value.__module__ == module.__name__ and issubclass(value, Exception)

    return {
        name: value
        for name, value in inspect.getmembers(module, inspect.isclass)
        if selected(value)
    }


def _policy_names(backend: str) -> set[str]:
    names: set[str] = set()
    for policy_names in BACKEND_POLICIES[backend].values():
        assert names.isdisjoint(policy_names)
        names.update(policy_names)
    return names


@pytest.mark.parametrize("backend", BACKEND_POLICIES)
def test_backend_exception_catalog_is_complete(backend: str) -> None:
    assert _policy_names(backend) == set(_backend_classes(backend))


def _urllib3_error(name: str, native_type: type[BaseException]) -> BaseException:
    pool = Mock()
    connection = Mock(host="example.test")
    if name in {"ClosedPoolError", "EmptyPoolError", "FullPoolError", "PoolError"}:
        return native_type(pool, "test")
    if name == "HeaderParsingError":
        return native_type([], b"test")
    if name == "HostChangedError":
        return native_type(pool, "http://example.test")
    if name == "IncompleteRead":
        return native_type(1, 2)
    if name == "InvalidChunkLength":
        return native_type(Mock(), b"x")
    if name == "MaxRetryError":
        return native_type(pool, "http://example.test")
    if name == "NameResolutionError":
        return native_type("example.test", connection, OSError("test"))
    if name == "NewConnectionError":
        return native_type(connection, "test")
    if name == "ProxyError":
        return native_type("test", OSError("test"))
    if name in {"ReadTimeoutError", "RequestError"}:
        return native_type(pool, "http://example.test", "test")
    return native_type("test")


def _aiohttp_error(name: str, native_type: type[BaseException]) -> BaseException:
    connection_key = Mock(host="example.test", port=443, ssl=True, is_ssl=True)
    connector_errors = {
        "ClientConnectorDNSError",
        "ClientConnectorError",
        "ClientConnectorSSLError",
        "ClientProxyConnectionError",
        "ClientSSLError",
    }
    response_errors = {
        "ClientHttpProxyError",
        "ClientResponseError",
        "ContentTypeError",
        "TooManyRedirects",
        "WSServerHandshakeError",
    }
    if name == "ClientConnectorCertificateError":
        return native_type(connection_key, ssl.CertificateError("test"))
    if name in connector_errors:
        return native_type(connection_key, OSError("test"))
    if name in response_errors:
        return native_type(Mock(real_url="http://example.test"), (), message="test")
    if name == "ServerFingerprintMismatch":
        return native_type(b"expected", b"actual", "example.test", 443)
    if name == "UnixClientConnectorError":
        return native_type("/tmp/test.sock", connection_key, OSError("test"))
    return native_type("test")


def _native_error(
    backend: str,
    name: str,
    native_type: type[BaseException],
) -> BaseException:
    catalog_backend = {
        "httpx_async": "httpx",
        "aiohttp_stream": "aiohttp",
    }.get(backend, backend)
    if catalog_backend == "urllib3":
        return _urllib3_error(name, native_type)
    if catalog_backend == "requests" and name == "JSONDecodeError":
        return native_type("test", "{}", 0)
    if catalog_backend == "httpx":
        if name == "HTTPStatusError":
            request = httpx.Request("GET", "http://example.test")
            response = httpx.Response(500, request=request, content=b"failed")
            return native_type("test", request=request, response=response)
        if issubclass(native_type, httpx.StreamError):
            return native_type("test") if name == "StreamError" else native_type()
    if catalog_backend == "aiohttp":
        return _aiohttp_error(name, native_type)
    return native_type("test")


def _run_sync_boundary(
    backend: str,
    native: BaseException,
    runtime: ModuleType,
) -> tuple[BaseException, int]:
    if backend == "http":
        module = __import__("contree_client.http", fromlist=["ContreeClient"])

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
        counter = connection
    else:

        class Backend:
            calls = 0

            def request(self, *args: object, **kwargs: object) -> None:
                self.calls += 1
                raise native

        counter = Backend()
        module = __import__(f"contree_client.{backend}", fromlist=["ContreeClient"])
        keyword = {
            "urllib3": "urllib3_pool_manager",
            "requests": "requests_session",
            "httpx": "httpx_client",
        }[backend]
        client = module.ContreeClient(
            "token",
            base_url="http://example.test",
            retry=runtime.RetryPolicy(delays=(0.0,), max_attempts=2),
            **{keyword: counter},
        )

    with pytest.raises(BaseException) as caught:
        client.call(runtime.RequestSpec(method="GET", path="/x", idempotent=True))
    return caught.value, counter.calls


def _run_aiohttp_boundary(
    native: BaseException,
    runtime: ModuleType,
) -> tuple[BaseException, int]:
    module = __import__("contree_client.aiohttp", fromlist=["ContreeAsyncClient"])

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
            await client.call(
                runtime.RequestSpec(method="GET", path="/x", idempotent=True)
            )
        return caught.value

    return asyncio.run(scenario()), session.calls


def _run_aiohttp_stream_boundary(
    native: BaseException,
    runtime: ModuleType,
) -> tuple[BaseException, int]:
    module = __import__("contree_client.aiohttp", fromlist=["ContreeAsyncClient"])

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
            aiohttp_session=session,
        )
        with pytest.raises(BaseException) as caught:
            await anext(client.stream(runtime.RequestSpec(method="GET", path="/x")))
        return caught.value

    return asyncio.run(scenario()), session.calls


def _run_httpx_async_boundary(
    native: BaseException,
    runtime: ModuleType,
) -> tuple[BaseException, int]:
    module = __import__("contree_client.httpx", fromlist=["ContreeAsyncClient"])

    class Backend:
        calls = 0

        async def request(self, *args: object, **kwargs: object) -> None:
            self.calls += 1
            raise native

    backend = Backend()

    async def scenario() -> BaseException:
        client = module.ContreeAsyncClient(
            "token",
            base_url="http://example.test",
            retry=runtime.RetryPolicy(delays=(0.0,), max_attempts=2),
            httpx_client=backend,
        )
        with pytest.raises(BaseException) as caught:
            await client.call(
                runtime.RequestSpec(method="GET", path="/x", idempotent=True)
            )
        return caught.value

    return asyncio.run(scenario()), backend.calls


def _run_boundary(
    backend: str,
    native: BaseException,
    runtime: ModuleType,
) -> tuple[BaseException, int]:
    if backend == "aiohttp":
        return _run_aiohttp_boundary(native, runtime)
    if backend == "aiohttp_stream":
        return _run_aiohttp_stream_boundary(native, runtime)
    if backend == "httpx_async":
        return _run_httpx_async_boundary(native, runtime)
    return _run_sync_boundary(backend, native, runtime)


def _matrix_cases() -> list[tuple[str, str, str]]:
    return [
        (adapter, name, policy)
        for backend, policies in BACKEND_POLICIES.items()
        for adapter in (
            (backend, "httpx_async")
            if backend == "httpx"
            else (backend, "aiohttp_stream")
            if backend == "aiohttp"
            else (backend,)
        )
        for policy, names in policies.items()
        for name in sorted(names)
    ]


@pytest.mark.parametrize(
    ("backend", "name", "policy"),
    _matrix_cases(),
    ids=lambda value: str(value),
)
def test_backend_exception_policy(
    backend: str,
    name: str,
    policy: str,
    runtime: ModuleType,
    exceptions: ModuleType,
) -> None:
    catalog_backend = {
        "httpx_async": "httpx",
        "aiohttp_stream": "aiohttp",
    }.get(backend, backend)
    native_type = _backend_classes(catalog_backend)[name]
    native = _native_error(backend, name, native_type)
    error, attempts = _run_boundary(backend, native, runtime)

    retryable_policies = {
        TRANSPORT_RETRYABLE,
        CONNECTION_RETRYABLE,
        TIMEOUT_RETRYABLE,
        DECOMPRESSION_RETRYABLE,
        API_RETRYABLE,
    }
    expected_attempts = 1
    if backend != "aiohttp_stream" and policy in retryable_policies:
        expected_attempts = 2
    assert attempts == expected_attempts
    if policy == NATIVE:
        assert error is native
        return

    expected_type = {
        TRANSPORT: exceptions.ContreeTransportError,
        TRANSPORT_RETRYABLE: exceptions.ContreeTransportError,
        CONNECTION: exceptions.ContreeConnectionError,
        CONNECTION_RETRYABLE: exceptions.ContreeConnectionError,
        TIMEOUT_RETRYABLE: exceptions.ContreeTimeoutError,
        DECOMPRESSION: exceptions.DecompressionError,
        DECOMPRESSION_RETRYABLE: exceptions.DecompressionError,
        API_RETRYABLE: exceptions.ContreeAPIError,
    }[policy]
    if policy == API_RETRYABLE:
        assert isinstance(error, expected_type)
    else:
        assert type(error) is expected_type
    assert error.original is native
    assert error.__cause__ is native
    assert not isinstance(error, native_type)
    if isinstance(error, exceptions.ContreeTransportError):
        assert error.retryable is (policy in retryable_policies)


@pytest.mark.parametrize(
    "backend",
    ["http", "urllib3", "requests", "httpx", "httpx_async", "aiohttp"],
)
@pytest.mark.parametrize(
    ("native", "expected_attempts"),
    [
        (OSError(errno.ECONNREFUSED, "refused"), 2),
        (OSError(errno.EACCES, "denied"), 1),
        (OSError(errno.EMFILE, "too many files"), 1),
        (OSError(errno.ENFILE, "system file table full"), 1),
        (OSError(errno.ENOMEM, "out of memory"), 1),
        (OSError(9999, "unknown"), 1),
        (socket.gaierror(socket.EAI_AGAIN, "temporary DNS failure"), 2),
        (socket.gaierror(socket.EAI_NONAME, "unknown host"), 1),
    ],
)
def test_direct_os_error_policy(
    backend: str,
    native: OSError,
    expected_attempts: int,
    runtime: ModuleType,
    exceptions: ModuleType,
) -> None:
    error, attempts = _run_boundary(backend, native, runtime)
    assert attempts == expected_attempts
    assert isinstance(error, exceptions.ContreeConnectionError)
    assert error.retryable is (expected_attempts == 2)
    assert error.original is native
    assert error.__cause__ is native


def test_winsock_code_is_classified_and_preserved(
    runtime: ModuleType,
    exceptions: ModuleType,
) -> None:
    native = OSError("winsock refused")
    native.winerror = 10061

    error, attempts = _run_sync_boundary("http", native, runtime)

    assert attempts == 2
    assert isinstance(error, exceptions.ContreeConnectionError)
    assert error.retryable is True
    assert error.original is native
    assert error.original.winerror == 10061


@pytest.mark.parametrize(
    "backend",
    ["http", "urllib3", "requests", "httpx", "httpx_async", "aiohttp"],
)
def test_adapter_bugs_are_not_wrapped(
    backend: str,
    runtime: ModuleType,
) -> None:
    native = AssertionError("adapter bug")
    error, attempts = _run_boundary(backend, native, runtime)
    assert attempts == 1
    assert error is native


@pytest.mark.parametrize("backend", ["http", "aiohttp"])
def test_untyped_header_validation_is_wrapped(
    backend: str,
    runtime: ModuleType,
    exceptions: ModuleType,
) -> None:
    native = ValueError("invalid header")
    error, attempts = _run_boundary(backend, native, runtime)
    assert attempts == 1
    assert isinstance(error, exceptions.ContreeTransportError)
    assert error.retryable is False
    assert error.original is native


@pytest.mark.parametrize("status", [200, 500])
def test_aiohttp_response_error_requires_error_status(
    status: int,
    runtime: ModuleType,
    exceptions: ModuleType,
) -> None:
    native = aiohttp.ClientResponseError(
        Mock(real_url="http://example.test"),
        (),
        status=status,
        message="test",
    )
    error, attempts = _run_aiohttp_boundary(native, runtime)
    expected_type = (
        exceptions.ContreeTransportError
        if status == 200
        else exceptions.ContreeAPIError
    )
    assert isinstance(error, expected_type)
    assert attempts == (1 if status == 200 else 2)


@pytest.mark.parametrize("status", [200, 500])
def test_requests_response_error_requires_error_status(
    status: int,
    runtime: ModuleType,
    exceptions: ModuleType,
) -> None:
    response = requests.Response()
    response.status_code = status
    response._content = b"test"
    native = requests.HTTPError("test", response=response)
    error, attempts = _run_sync_boundary("requests", native, runtime)
    expected_type = (
        exceptions.ContreeTransportError
        if status == 200
        else exceptions.ContreeAPIError
    )
    assert isinstance(error, expected_type)
    assert attempts == (1 if status == 200 else 2)
