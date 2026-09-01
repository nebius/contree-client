"""The transport-error tree and its per-backend native-exception mapping."""

from __future__ import annotations

import gzip
import http.client
import importlib
import socket
import ssl
from types import ModuleType
from unittest.mock import Mock

import pytest
from yarl import URL


def test_tree_shape(exceptions: ModuleType) -> None:
    assert issubclass(exceptions.ContreeTransportError, exceptions.ContreeError)
    assert issubclass(
        exceptions.ContreeConnectionError, exceptions.ContreeTransportError
    )
    assert issubclass(exceptions.ContreeSSLError, exceptions.ContreeConnectionError)
    assert issubclass(
        exceptions.ContreeConnectionClosedError, exceptions.ContreeConnectionError
    )
    assert issubclass(exceptions.ContreeTimeoutError, exceptions.ContreeTransportError)
    assert issubclass(exceptions.ContreeTimeoutError, TimeoutError)
    assert issubclass(exceptions.ContreeProtocolError, exceptions.ContreeTransportError)
    assert issubclass(exceptions.ContreeStreamError, exceptions.ContreeProtocolError)
    assert issubclass(exceptions.ContreeHTTPError, exceptions.ContreeTransportError)
    assert issubclass(exceptions.DecompressionError, exceptions.ContreeStreamError)


def test_original_defaults_to_none(exceptions: ModuleType) -> None:
    assert exceptions.ContreeError("x").original is None
    assert exceptions.ContreeTransportError("x").original is None


def test_original_mirrors_cause(exceptions: ModuleType) -> None:
    cause = ValueError("boom")
    err = exceptions.ContreeError("x")
    try:
        raise err from cause
    except exceptions.ContreeError as raised:
        assert raised.original is cause
        assert str(raised) == "x"


@pytest.mark.parametrize(
    ("name", "message"),
    [
        ("ContreeConnectionError", "Connection failed"),
        ("ContreeSSLError", "TLS connection failed"),
        ("ContreeConnectionClosedError", "Peer closed the connection"),
        ("ContreeTimeoutError", "Request timed out"),
        ("ContreeProtocolError", "Protocol error"),
        ("DecompressionError", "Response decompression failed"),
    ],
)
def test_empty_transport_errors_have_readable_messages(
    exceptions: ModuleType, name: str, message: str
) -> None:
    assert str(getattr(exceptions, name)()) == message


def test_wrap_sets_original_and_fallback_message(exceptions: ModuleType) -> None:
    native = ConnectionError()
    wrapped = exceptions.ContreeConnectionError.wrap(native)
    assert isinstance(wrapped, exceptions.ContreeConnectionError)
    assert wrapped.original is native
    assert str(wrapped) == "Connection failed"


def test_stream_error_remains_a_top_level_export(
    generated_package: ModuleType, exceptions: ModuleType
) -> None:
    assert generated_package.ContreeStreamError is exceptions.ContreeStreamError


def test_api_error_status_and_retry_after_unchanged(exceptions: ModuleType) -> None:
    err = exceptions.ContreeAPIError(404, "not found")
    assert err.status == 404
    assert err.retry_after is None
    assert not hasattr(exceptions.ContreeTransportError("x"), "status")

    gone = exceptions.GoneError(410, "gone", retry_after=5)
    assert gone.retry_after == 5


class TestHttpBackend:
    @pytest.fixture(autouse=True)
    def setup(self, generated_package: ModuleType, exceptions: ModuleType) -> None:
        self.module = importlib.import_module("contree_client.http")
        self.exceptions = exceptions

    def test_connection_closed(self) -> None:
        native = http.client.RemoteDisconnected("closed")
        result = self.module.translate_error(native)
        assert isinstance(result, self.exceptions.ContreeConnectionClosedError)
        assert isinstance(result, self.module.ContreeHttpConnectionError)
        assert isinstance(result, http.client.RemoteDisconnected)
        assert result.original is native
        assert str(result) == str(native)

    def test_ssl(self) -> None:
        native = ssl.SSLError("bad cert")
        result = self.module.translate_error(native)
        assert isinstance(result, self.exceptions.ContreeSSLError)
        assert isinstance(result, self.module.ContreeHttpConnectionError)
        assert isinstance(result, ssl.SSLError)
        assert isinstance(result, self.module.ContreeClient.nonretryable_errors)
        assert str(result) == str(native)

    def test_timeout(self) -> None:
        result = self.module.translate_error(TimeoutError())
        assert isinstance(result, self.exceptions.ContreeTimeoutError)
        assert isinstance(result, TimeoutError)
        assert isinstance(result, self.module.ContreeClient.retryable_errors)
        assert not isinstance(result, self.module.ContreeClient.nonretryable_errors)
        assert str(result) == "Request timed out"

    def test_gaierror_becomes_connection_error_not_socket_gaierror(self) -> None:
        native = socket.gaierror("dns failed")
        result = self.module.translate_error(native)
        # the representative native ancestor is builtin ConnectionError,
        # not socket.gaierror - the accepted granularity trade-off
        assert isinstance(result, self.exceptions.ContreeConnectionError)
        assert not isinstance(result, socket.gaierror)
        assert result.original is native

    def test_bad_status_line(self) -> None:
        native = http.client.BadStatusLine("garbage")
        result = self.module.translate_error(native)
        assert isinstance(result, self.exceptions.ContreeProtocolError)
        assert isinstance(result, self.module.ContreeHttpConnectionError)
        assert isinstance(result, http.client.BadStatusLine)
        assert result.line == native.line
        assert str(result) == str(native)

    def test_invalid_url_remains_nonretryable(self) -> None:
        native = http.client.InvalidURL("URL contains a control character")
        result = self.module.translate_error(native)
        assert isinstance(result, self.exceptions.ContreeProtocolError)
        assert isinstance(result, self.module.ContreeHttpConnectionError)
        assert isinstance(result, http.client.InvalidURL)
        assert isinstance(result, self.module.ContreeClient.nonretryable_errors)
        assert result.original is native
        assert str(result) == str(native)

    def test_incomplete_read_keeps_native_message(self) -> None:
        native = http.client.IncompleteRead(b"partial", 9)
        result = self.module.translate_error(native)
        assert isinstance(result, self.exceptions.ContreeProtocolError)
        assert isinstance(result, self.module.ContreeHttpConnectionError)
        assert isinstance(result, http.client.IncompleteRead)
        assert result.partial == native.partial
        assert result.expected == native.expected
        assert str(result) == "IncompleteRead(7 bytes read, 9 more expected)"

    def test_line_too_long_is_a_protocol_error(self) -> None:
        native = http.client.LineTooLong("header line")
        result = self.module.translate_error(native)
        assert isinstance(result, self.exceptions.ContreeProtocolError)
        assert isinstance(result, self.module.ContreeHttpConnectionError)
        assert isinstance(result, http.client.HTTPException)
        assert str(result) == str(native)

    def test_bad_gzip_is_a_decompression_error(self) -> None:
        native = gzip.BadGzipFile("not a gzip file")
        result = self.module.translate_error(native)
        assert isinstance(result, self.exceptions.DecompressionError)
        assert isinstance(result, self.module.ContreeHttpConnectionError)
        assert isinstance(result, gzip.BadGzipFile)
        assert str(result) == str(native)

    def test_unmatched_exception_passes_through_unwrapped(self) -> None:
        native = ArithmeticError("not a transport error at all")
        result = self.module.translate_error(native)
        assert result is native


class TestHttpxBackend:
    @pytest.fixture(autouse=True)
    def setup(self, generated_package: ModuleType, exceptions: ModuleType) -> None:
        pytest.importorskip("httpx")
        self.httpx = importlib.import_module("httpx")
        self.module = importlib.import_module("contree_client.httpx")
        self.exceptions = exceptions

    def test_timeout(self) -> None:
        result = self.module.translate_error(self.httpx.ConnectTimeout("x"))
        assert isinstance(result, self.exceptions.ContreeTimeoutError)
        assert isinstance(result, self.httpx.TimeoutException)
        assert isinstance(result, self.module.ContreeClient.retryable_errors)
        assert isinstance(result, self.module.ContreeAsyncClient.retryable_errors)
        assert not isinstance(result, self.module.ContreeClient.nonretryable_errors)
        assert not isinstance(
            result, self.module.ContreeAsyncClient.nonretryable_errors
        )

        request = self.httpx.Request("GET", "https://example.com")
        direct = self.module.ContreeHttpxTimeoutError("x", request=request)
        assert direct.request is request

    def test_remote_protocol_error(self) -> None:
        native = self.httpx.RemoteProtocolError("peer sent malformed HTTP")
        result = self.module.translate_error(native)
        assert isinstance(result, self.exceptions.ContreeProtocolError)
        assert isinstance(result, self.exceptions.ContreeConnectionClosedError)
        assert isinstance(result, self.module.ContreeHttpxConnectionError)
        assert isinstance(result, self.httpx.RemoteProtocolError)
        assert str(result) == str(native)

    def test_local_protocol_error(self) -> None:
        native = self.httpx.LocalProtocolError("bad")
        result = self.module.translate_error(native)
        assert isinstance(result, self.exceptions.ContreeProtocolError)
        assert not isinstance(result, self.exceptions.ContreeConnectionClosedError)
        assert isinstance(result, self.module.ContreeHttpxConnectionError)
        assert isinstance(result, self.httpx.LocalProtocolError)
        assert isinstance(result, self.module.ContreeClient.nonretryable_errors)
        assert isinstance(result, self.module.ContreeAsyncClient.nonretryable_errors)
        assert str(result) == str(native)

    def test_ssl_via_realistic_nested_cause(self) -> None:
        httpcore = importlib.import_module("httpcore")
        request = self.httpx.Request("GET", "https://example.com")
        core_error = httpcore.ConnectError("tls failed")
        core_error.__cause__ = ssl.SSLError("certificate verify failed")
        connect_error = self.httpx.ConnectError("tls failed", request=request)
        connect_error.__cause__ = core_error
        result = self.module.translate_error(connect_error)
        assert isinstance(result, self.exceptions.ContreeSSLError)
        assert isinstance(result, self.module.ContreeHttpxConnectionError)
        assert isinstance(result, self.httpx.ConnectError)
        assert result.original is connect_error
        assert result.request is request
        assert isinstance(result, self.module.ContreeClient.nonretryable_errors)
        assert str(result) == str(connect_error)

    def test_empty_timeout_has_fallback_message(self) -> None:
        result = self.module.translate_error(self.httpx.ConnectTimeout(""))
        assert isinstance(result, self.exceptions.ContreeTimeoutError)
        assert str(result) == "Request timed out"

    def test_decoding_error(self) -> None:
        request = self.httpx.Request("GET", "https://example.com")
        native = self.httpx.DecodingError("invalid gzip", request=request)
        result = self.module.translate_error(native)
        assert isinstance(result, self.exceptions.DecompressionError)
        assert isinstance(result, self.httpx.DecodingError)
        assert result.request is request
        assert str(result) == str(native)

    def test_connect_error_without_ssl_cause_is_plain_connection_error(self) -> None:
        result = self.module.translate_error(self.httpx.ConnectError("refused"))
        assert isinstance(result, self.exceptions.ContreeConnectionError)
        assert not isinstance(result, self.exceptions.ContreeSSLError)

    def test_status_error_carries_response_status(self) -> None:
        request = self.httpx.Request("GET", "https://example.com")
        response = self.httpx.Response(404, request=request)
        native = self.httpx.HTTPStatusError("404", request=request, response=response)
        result = self.module.translate_error(native)
        assert isinstance(result, self.exceptions.ContreeHTTPError)
        assert isinstance(result, self.httpx.HTTPStatusError)
        assert result.status == 404
        assert result.original is native

    def test_unmatched_exception_passes_through_unwrapped(self) -> None:
        native = self.httpx.CookieConflict("x")
        result = self.module.translate_error(native)
        assert result is native


class TestRequestsBackend:
    @pytest.fixture(autouse=True)
    def setup(self, generated_package: ModuleType, exceptions: ModuleType) -> None:
        pytest.importorskip("requests")
        self.requests = importlib.import_module("requests")
        self.module = importlib.import_module("contree_client.requests")
        self.exceptions = exceptions

    def test_connect_timeout_is_timeout_not_connection(self) -> None:
        result = self.module.translate_error(
            self.requests.exceptions.ConnectTimeout("x")
        )
        assert isinstance(result, self.exceptions.ContreeTimeoutError)
        assert not isinstance(result, self.exceptions.ContreeConnectionError)
        assert isinstance(result, self.module.ContreeClient.retryable_errors)
        assert not isinstance(result, self.module.ContreeClient.nonretryable_errors)

    def test_read_timeout_is_retryable(self) -> None:
        result = self.module.translate_error(self.requests.exceptions.ReadTimeout("x"))
        assert isinstance(result, self.exceptions.ContreeTimeoutError)
        assert isinstance(result, self.module.ContreeClient.retryable_errors)
        assert not isinstance(result, self.module.ContreeClient.nonretryable_errors)

    def test_ssl(self) -> None:
        request = self.requests.Request("GET", "https://example.com").prepare()
        response = self.requests.Response()
        native = self.requests.exceptions.SSLError(
            "bad cert", request=request, response=response
        )
        result = self.module.translate_error(native)
        assert isinstance(result, self.exceptions.ContreeSSLError)
        assert isinstance(result, self.module.ContreeRequestsConnectionError)
        assert isinstance(result, self.requests.exceptions.SSLError)
        assert result.request is request
        assert result.response is response
        assert isinstance(result, self.module.ContreeClient.nonretryable_errors)
        assert str(result) == str(native)

    def test_connection_error(self) -> None:
        result = self.module.translate_error(
            self.requests.exceptions.ConnectionError("refused")
        )
        assert isinstance(result, self.exceptions.ContreeConnectionError)

    def test_chunked_encoding_is_protocol_not_http_error(self) -> None:
        native = self.requests.exceptions.ChunkedEncodingError("x")
        result = self.module.translate_error(native)
        assert isinstance(result, self.exceptions.ContreeProtocolError)
        assert not isinstance(result, self.exceptions.ContreeHTTPError)
        assert isinstance(result, self.requests.exceptions.ChunkedEncodingError)
        assert str(result) == str(native)

    def test_chunked_encoding_nested_error_is_readable(self) -> None:
        urllib3 = importlib.import_module("urllib3")
        nested = urllib3.exceptions.ProtocolError(
            "Connection broken", http.client.IncompleteRead(b"partial", 9)
        )
        native = self.requests.exceptions.ChunkedEncodingError(nested)
        result = self.module.translate_error(native)
        assert str(result) == (
            "Connection broken: IncompleteRead(7 bytes read, 9 more expected)"
        )

    def test_content_decoding_error(self) -> None:
        request = self.requests.Request("GET", "https://example.com").prepare()
        response = self.requests.Response()
        native = self.requests.exceptions.ContentDecodingError(
            "invalid gzip", request=request, response=response
        )
        result = self.module.translate_error(native)
        assert isinstance(result, self.exceptions.DecompressionError)
        assert isinstance(result, self.requests.exceptions.ContentDecodingError)
        assert result.request is request
        assert result.response is response
        assert str(result) == str(native)

    def test_http_error_carries_response_status(self) -> None:
        response = self.requests.Response()
        response.status_code = 503
        native = self.requests.exceptions.HTTPError(
            "service unavailable", response=response
        )
        direct = self.module.ContreeRequestsAPIError.wrap(native)
        assert isinstance(direct, self.module.ContreeRequestsAPIError)
        assert direct.status == 503
        assert direct.original is native
        result = self.module.translate_error(native)
        assert isinstance(result, self.exceptions.ContreeHTTPError)
        assert isinstance(result, self.requests.exceptions.HTTPError)
        assert result.status == 503

    def test_http_error_without_response_passes_through(self) -> None:
        native = self.requests.exceptions.HTTPError("no response attached")
        result = self.module.translate_error(native)
        assert result is native

    def test_unmatched_exception_passes_through_unwrapped(self) -> None:
        native = self.requests.exceptions.RequestException("x")
        result = self.module.translate_error(native)
        assert result is native


class TestAiohttpBackend:
    @pytest.fixture(autouse=True)
    def setup(self, generated_package: ModuleType, exceptions: ModuleType) -> None:
        pytest.importorskip("aiohttp")
        self.aiohttp = importlib.import_module("aiohttp")
        self.module = importlib.import_module("contree_client.aiohttp")
        self.exceptions = exceptions

    def test_server_timeout(self) -> None:
        result = self.module.translate_error(self.aiohttp.ServerTimeoutError())
        assert isinstance(result, self.exceptions.ContreeTimeoutError)
        assert isinstance(result, self.aiohttp.ServerTimeoutError)
        assert isinstance(result, self.module.ContreeAsyncClient.retryable_errors)
        assert not isinstance(
            result, self.module.ContreeAsyncClient.nonretryable_errors
        )
        assert str(result) == "Request timed out"

    def test_bare_timeout_error(self) -> None:
        result = self.module.translate_error(TimeoutError("x"))
        assert isinstance(result, self.exceptions.ContreeTimeoutError)
        assert isinstance(result, self.module.ContreeAsyncClient.retryable_errors)
        assert not isinstance(
            result, self.module.ContreeAsyncClient.nonretryable_errors
        )

    def test_server_disconnected(self) -> None:
        native = self.aiohttp.ServerDisconnectedError("server disconnected")
        result = self.module.translate_error(native)
        assert isinstance(result, self.exceptions.ContreeConnectionClosedError)
        assert isinstance(result, self.module.ContreeAiohttpConnectionError)
        assert isinstance(result, self.aiohttp.ServerDisconnectedError)
        assert str(result) == str(native)

    def test_connector_error(self) -> None:
        conn_key = Mock(ssl=False, host="example.com", port=443)
        native = self.aiohttp.ClientConnectorError(conn_key, OSError("refused"))
        result = self.module.translate_error(native)
        assert isinstance(result, self.exceptions.ContreeConnectionError)
        assert isinstance(result, self.aiohttp.ClientConnectionError)
        assert str(result) == str(native)

    def test_ssl_error(self) -> None:
        conn_key = Mock(ssl=True, host="example.com", port=443, is_ssl=True)
        native = self.aiohttp.ClientConnectorSSLError(conn_key, ssl.SSLError("boom"))
        result = self.module.translate_error(native)
        assert isinstance(result, self.exceptions.ContreeSSLError)
        assert isinstance(result, self.module.ContreeAiohttpConnectionError)
        assert isinstance(result, self.aiohttp.ClientSSLError)
        assert result.original is native
        assert isinstance(result, self.module.ContreeAsyncClient.nonretryable_errors)
        assert str(result) == str(native)

    def test_certificate_error_keeps_native_diagnostic(self) -> None:
        conn_key = Mock(ssl=True, host="example.com", port=443, is_ssl=True)
        native = self.aiohttp.ClientConnectorCertificateError(
            conn_key, ssl.CertificateError("hostname mismatch")
        )
        result = self.module.translate_error(native)
        assert isinstance(result, self.exceptions.ContreeSSLError)
        assert isinstance(result, self.module.ContreeAiohttpConnectionError)
        assert "hostname mismatch" in str(result)
        assert str(result) == str(native)

    def test_fingerprint_mismatch_has_readable_diagnostic(self) -> None:
        native = self.aiohttp.ServerFingerprintMismatch(
            b"expected", b"actual", "example.com", 443
        )
        result = self.module.translate_error(native)
        assert isinstance(result, self.exceptions.ContreeSSLError)
        assert isinstance(result, self.module.ContreeAiohttpConnectionError)
        assert isinstance(result, self.aiohttp.ServerFingerprintMismatch)
        assert isinstance(result, self.module.ContreeAsyncClient.nonretryable_errors)
        assert str(result) == (
            "TLS fingerprint mismatch for example.com:443: "
            "expected 6578706563746564, got 61637475616c"
        )

    def test_payload_error(self) -> None:
        result = self.module.translate_error(self.aiohttp.ClientPayloadError("x"))
        assert isinstance(result, self.exceptions.ContreeProtocolError)

    def test_response_error_carries_status(self) -> None:
        request_info = self.aiohttp.RequestInfo(
            url=URL("https://example.com"),
            method="GET",
            headers={},
            real_url=URL("https://example.com"),
        )
        native = self.aiohttp.ClientResponseError(
            request_info=request_info,
            history=(),
            status=403,
            message="forbidden",
            headers={"x-request-id": "request-1"},
        )
        result = self.module.translate_error(native)
        assert isinstance(result, self.exceptions.ContreeHTTPError)
        assert isinstance(result, self.aiohttp.ClientResponseError)
        assert result.status == 403
        assert result.message == "forbidden"
        assert result.headers == native.headers
        assert result.original is native
        assert str(result) == str(native)

    def test_unmatched_exception_passes_through_unwrapped(self) -> None:
        native = self.aiohttp.WSMessageTypeError("x")
        result = self.module.translate_error(native)
        assert result is native


class TestUrllib3Backend:
    @pytest.fixture(autouse=True)
    def setup(self, generated_package: ModuleType, exceptions: ModuleType) -> None:
        pytest.importorskip("urllib3")
        self.urllib3 = importlib.import_module("urllib3")
        self.module = importlib.import_module("contree_client.urllib3")
        self.exceptions = exceptions

    def test_new_connection_error(self) -> None:
        native = self.urllib3.exceptions.NewConnectionError(None, "boom")
        result = self.module.translate_error(native)
        assert isinstance(result, self.exceptions.ContreeConnectionError)
        assert isinstance(result, self.urllib3.exceptions.NewConnectionError)
        assert isinstance(result, self.module.ContreeClient.retryable_errors)
        assert not isinstance(result, self.module.ContreeClient.nonretryable_errors)
        assert result.original is native

    def test_timeout(self) -> None:
        result = self.module.translate_error(
            self.urllib3.exceptions.ConnectTimeoutError("timed out")
        )
        assert isinstance(result, self.exceptions.ContreeTimeoutError)
        assert isinstance(result, self.module.ContreeClient.retryable_errors)
        assert not isinstance(result, self.module.ContreeClient.nonretryable_errors)

    def test_ssl(self) -> None:
        native = self.urllib3.exceptions.SSLError("bad cert")
        result = self.module.translate_error(native)
        assert isinstance(result, self.exceptions.ContreeSSLError)
        assert isinstance(result, self.module.ContreeUrllib3ConnectionError)
        assert isinstance(result, self.urllib3.exceptions.SSLError)
        assert isinstance(result, self.module.ContreeClient.nonretryable_errors)
        assert str(result) == str(native)

    def test_protocol_error_with_connection_cause_is_closed(self) -> None:
        native = self.urllib3.exceptions.ProtocolError(
            "Connection aborted.", ConnectionResetError("reset")
        )
        result = self.module.translate_error(native)
        assert isinstance(result, self.exceptions.ContreeConnectionClosedError)
        assert isinstance(result, self.module.ContreeUrllib3ConnectionError)
        assert isinstance(result, self.urllib3.exceptions.ProtocolError)
        assert str(result) == "Connection aborted. reset"

    def test_protocol_error_without_cause_is_plain_protocol(self) -> None:
        native = self.urllib3.exceptions.ProtocolError("garbled")
        result = self.module.translate_error(native)
        assert isinstance(result, self.exceptions.ContreeProtocolError)
        assert not isinstance(result, self.exceptions.ContreeConnectionClosedError)
        assert isinstance(result, self.module.ContreeUrllib3ConnectionError)
        assert isinstance(result, self.urllib3.exceptions.ProtocolError)
        assert str(result) == str(native)

    def test_decode_error(self) -> None:
        native = self.urllib3.exceptions.DecodeError("invalid gzip")
        result = self.module.translate_error(native)
        assert isinstance(result, self.exceptions.DecompressionError)
        assert isinstance(result, self.module.ContreeUrllib3ConnectionError)
        assert isinstance(result, self.urllib3.exceptions.DecodeError)
        assert str(result) == str(native)

    def test_generic_http_error_is_connection_error(self) -> None:
        native = self.urllib3.exceptions.HTTPError("generic")
        result = self.module.translate_error(native)
        assert isinstance(result, self.exceptions.ContreeConnectionError)
        assert isinstance(result, self.module.ContreeUrllib3ConnectionError)
        assert isinstance(result, self.urllib3.exceptions.HTTPError)
        assert result.original is native

    @pytest.mark.parametrize(
        "native_type",
        [
            "MaxRetryError",
            "ResponseError",
            "ClosedPoolError",
            "LocationValueError",
            "LocationParseError",
        ],
    )
    def test_other_http_errors_use_generic_connection_fallback(
        self, native_type: str
    ) -> None:
        native_class = getattr(self.urllib3.exceptions, native_type)
        assert (
            self.module.translate_exc_class(native_class)
            is self.module.ContreeUrllib3ConnectionError
        )

    def test_non_http_exception_passes_through_unwrapped(self) -> None:
        native = ValueError("generic")
        result = self.module.translate_error(native)
        assert result is native
