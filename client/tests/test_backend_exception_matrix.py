"""Exhaustive policy matrix for exceptions exported by HTTP backends."""

from __future__ import annotations

import asyncio
import http.client
import inspect
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
CONNECTION = "connection"
CONNECTION_NONRETRYABLE = "connection-nonretryable"
TIMEOUT = "timeout"
STREAM = "stream"
DECOMPRESSION = "decompression"
DECOMPRESSION_NONRETRYABLE = "decompression-nonretryable"


# Each public exception name exported by the installed backend belongs to one
# policy. The catalog test below fails when a backend changes its exception
# surface, so a dependency update requires an explicit policy decision.
BACKEND_POLICIES = {
    "http": {
        NATIVE: {"InvalidURL"},
        CONNECTION: {
            "BadStatusLine",
            "CannotSendHeader",
            "CannotSendRequest",
            "HTTPException",
            "ImproperConnectionState",
            "IncompleteRead",
            "LineTooLong",
            "NotConnected",
            "RemoteDisconnected",
            "ResponseNotReady",
            "UnimplementedFileMode",
            "UnknownProtocol",
            "UnknownTransferEncoding",
            "error",
        },
    },
    "urllib3": {
        NATIVE: {
            "InvalidHeader",
            "LocationParseError",
            "LocationValueError",
            "ProxySchemeUnknown",
            "URLSchemeUnknown",
        },
        CONNECTION: {
            "BodyNotHttplibCompatible",
            "ClosedPoolError",
            "ConnectionError",
            "EmptyPoolError",
            "FullPoolError",
            "HTTPError",
            "HeaderParsingError",
            "HostChangedError",
            "IncompleteRead",
            "InvalidChunkLength",
            "MaxRetryError",
            "NameResolutionError",
            "NewConnectionError",
            "PoolError",
            "ProtocolError",
            "ProxyError",
            "RequestError",
            "ResponseError",
            "ResponseNotChunked",
            "TimeoutStateError",
            "UnrewindableBodyError",
        },
        CONNECTION_NONRETRYABLE: {"SSLError"},
        TIMEOUT: {"ConnectTimeoutError", "ReadTimeoutError", "TimeoutError"},
        DECOMPRESSION: {"DecodeError"},
    },
    "requests": {
        NATIVE: {
            "HTTPError",
            "InvalidHeader",
            "InvalidJSONError",
            "InvalidProxyURL",
            "InvalidSchema",
            "InvalidURL",
            "JSONDecodeError",
            "MissingSchema",
            "RequestException",
            "RetryError",
            "StreamConsumedError",
            "TooManyRedirects",
            "URLRequired",
            "UnrewindableBodyError",
        },
        CONNECTION: {"ConnectionError", "ProxyError"},
        CONNECTION_NONRETRYABLE: {"SSLError"},
        TIMEOUT: {"ConnectTimeout", "ReadTimeout", "Timeout"},
        STREAM: {"ChunkedEncodingError"},
        DECOMPRESSION_NONRETRYABLE: {"ContentDecodingError"},
    },
    "httpx": {
        NATIVE: {
            "CookieConflict",
            "HTTPError",
            "HTTPStatusError",
            "InvalidURL",
            "LocalProtocolError",
            "RequestError",
            "RequestNotRead",
            "ResponseNotRead",
            "StreamClosed",
            "StreamConsumed",
            "StreamError",
            "TooManyRedirects",
            "UnsupportedProtocol",
        },
        CONNECTION: {
            "CloseError",
            "ConnectError",
            "NetworkError",
            "ProtocolError",
            "ProxyError",
            "ReadError",
            "RemoteProtocolError",
            "TransportError",
            "WriteError",
        },
        TIMEOUT: {
            "ConnectTimeout",
            "PoolTimeout",
            "ReadTimeout",
            "TimeoutException",
            "WriteTimeout",
        },
        DECOMPRESSION_NONRETRYABLE: {"DecodingError"},
    },
    "aiohttp": {
        NATIVE: {
            "ClientError",
            "ClientHttpProxyError",
            "ClientResponseError",
            "ContentTypeError",
            "InvalidURL",
            "InvalidUrlClientError",
            "InvalidUrlRedirectClientError",
            "NonHttpUrlClientError",
            "NonHttpUrlRedirectClientError",
            "RedirectClientError",
            "TooManyRedirects",
            "WSMessageTypeError",
            "WSServerHandshakeError",
        },
        CONNECTION: {
            "ClientConnectionError",
            "ClientConnectionResetError",
            "ClientConnectorDNSError",
            "ClientConnectorError",
            "ClientOSError",
            "ClientProxyConnectionError",
            "ServerConnectionError",
            "ServerDisconnectedError",
            "UnixClientConnectorError",
        },
        CONNECTION_NONRETRYABLE: {
            "ClientConnectorCertificateError",
            "ClientConnectorSSLError",
            "ClientSSLError",
            "ServerFingerprintMismatch",
        },
        TIMEOUT: {
            "ConnectionTimeoutError",
            "ServerTimeoutError",
            "SocketTimeoutError",
        },
        STREAM: {"ClientPayloadError"},
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
    connection_key = Mock(
        host="example.test",
        port=443,
        ssl=True,
        is_ssl=True,
    )
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
    backend = "httpx" if backend == "httpx_async" else backend
    if backend == "urllib3":
        return _urllib3_error(name, native_type)
    if backend == "requests" and name == "JSONDecodeError":
        return native_type("test", "{}", 0)
    if backend == "httpx":
        if name == "HTTPStatusError":
            request = httpx.Request("GET", "http://example.test")
            response = httpx.Response(500, request=request)
            return native_type("test", request=request, response=response)
        if issubclass(native_type, httpx.StreamError):
            return native_type("test") if name == "StreamError" else native_type()
    if backend == "aiohttp":
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


def _matrix_cases() -> list[tuple[str, str, str]]:
    return [
        (adapter, name, policy)
        for backend, policies in BACKEND_POLICIES.items()
        for adapter in ((backend, "httpx_async") if backend == "httpx" else (backend,))
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
    catalog_backend = "httpx" if backend == "httpx_async" else backend
    native_type = _backend_classes(catalog_backend)[name]
    native = _native_error(backend, name, native_type)
    if backend == "aiohttp":
        error, attempts = _run_aiohttp_boundary(native, runtime)
    elif backend == "httpx_async":
        error, attempts = _run_httpx_async_boundary(native, runtime)
    else:
        error, attempts = _run_sync_boundary(backend, native, runtime)

    expected_attempts = (
        2
        if policy
        in {
            CONNECTION,
            TIMEOUT,
            STREAM,
            DECOMPRESSION,
        }
        else 1
    )
    assert attempts == expected_attempts
    if policy == NATIVE:
        assert error is native
        return

    expected_type = {
        CONNECTION: exceptions.ContreeConnectionError,
        CONNECTION_NONRETRYABLE: exceptions.ContreeConnectionError,
        TIMEOUT: exceptions.ContreeTimeoutError,
        STREAM: exceptions.ContreeStreamError,
        DECOMPRESSION: exceptions.DecompressionError,
        DECOMPRESSION_NONRETRYABLE: exceptions.DecompressionError,
    }[policy]
    assert isinstance(error, expected_type)
    assert error.original is native
    assert error.__cause__ is native

    native_base = {
        "http": OSError,
        "urllib3": urllib3.exceptions.HTTPError,
        "requests": requests.ConnectionError,
        "httpx": httpx.TransportError,
        "aiohttp": aiohttp.ClientConnectionError,
    }
    if policy in {CONNECTION, CONNECTION_NONRETRYABLE}:
        assert isinstance(error, native_base[catalog_backend])
    elif policy == TIMEOUT:
        timeout_base = {
            "http": TimeoutError,
            "urllib3": urllib3.exceptions.TimeoutError,
            "requests": requests.Timeout,
            "httpx": httpx.TimeoutException,
            "aiohttp": TimeoutError,
        }
        assert isinstance(error, timeout_base[catalog_backend])
