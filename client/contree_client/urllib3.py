"""Contree API client backed by urllib3."""

from __future__ import annotations

import ssl
from collections.abc import Iterator
from functools import lru_cache

import urllib3

from . import base
from .exceptions import (
    ContreeConnectionClosedError,
    ContreeConnectionError,
    ContreeProtocolError,
    ContreeSSLError,
    ContreeTimeoutError,
    ContreeTransportError,
    DecompressionError,
)
from .runtime import (
    CHUNK_SIZE,
    RequestSpec,
    ResponseData,
    RetryPolicy,
    error_for_response,
    library_version,
)
from .spec_info import DEFAULT_BASE_URL
from .types import logger


class ContreeUrllib3ConnectionError(
    ContreeConnectionError, urllib3.exceptions.HTTPError
):
    """A `ContreeConnectionError` that is also a `urllib3.exceptions.HTTPError`."""


class ContreeUrllib3TimeoutError(ContreeTimeoutError, urllib3.exceptions.TimeoutError):
    """A `ContreeTimeoutError` that is also a `urllib3.exceptions.TimeoutError`."""


class ContreeUrllib3NewConnectionError(
    ContreeUrllib3ConnectionError, urllib3.exceptions.NewConnectionError
):
    """A `ContreeUrllib3ConnectionError` that is also a
    `urllib3.exceptions.NewConnectionError`.

    This is a subclass of `ContreeUrllib3ConnectionError`, not a
    sibling. Existing `except ContreeUrllib3ConnectionError` call
    sites still match it.
    """

    @classmethod
    def wrap(cls, original: BaseException) -> BaseException:
        """Build from the native instance's own attributes, not `.args`.

        `NewConnectionError.__init__(conn, message)` needs two
        positional arguments. `.args` collapses them into one combined
        string.
        """
        if not isinstance(original, urllib3.exceptions.NewConnectionError):
            return original
        try:
            wrapped = cls(original.conn, original._message)
        except Exception:
            return original
        wrapped.__cause__ = original
        return wrapped


class ContreeUrllib3SSLError(
    ContreeSSLError, ContreeUrllib3ConnectionError, urllib3.exceptions.SSLError
):
    """A `ContreeSSLError` that is also a `urllib3.exceptions.SSLError`."""


class ContreeUrllib3ProtocolError(
    ContreeProtocolError,
    ContreeUrllib3ConnectionError,
    urllib3.exceptions.ProtocolError,
):
    """A protocol error catchable through both existing hierarchies."""

    def __str__(self) -> str:
        original = self.original
        if (
            isinstance(original, urllib3.exceptions.ProtocolError)
            and len(original.args) > 1
            and isinstance(original.args[1], BaseException)
        ):
            summary = str(original.args[0]).strip()
            detail = str(original.args[1]).strip()
            if summary and detail:
                return f"{summary} {detail}"
        return super().__str__()


class ContreeUrllib3ConnectionClosedError(
    ContreeConnectionClosedError, ContreeUrllib3ProtocolError
):
    """A reset connection catchable through both existing hierarchies."""

    def __str__(self) -> str:
        return ContreeUrllib3ProtocolError.__str__(self)


class ContreeUrllib3DecompressionError(
    DecompressionError,
    ContreeUrllib3ConnectionError,
    urllib3.exceptions.DecodeError,
):
    """A decoding error catchable through both existing hierarchies."""


@lru_cache
def translate_exc_class(
    exc_type: type[BaseException],
) -> type[ContreeTransportError] | None:
    """Return the Contree hybrid class for a native exception type.

    Return None when the exception is not an urllib3 HTTP error. The
    cache resolves each distinct type once, not on every call.

    `NewConnectionError` subclasses `ConnectTimeoutError` in urllib3's
    own hierarchy. Match it first.

    This function does not cover `ProtocolError` with a
    connection-reset cause: that depends on `__cause__`, not the type.
    `translate_error()` handles it before it calls this cache.
    """
    if issubclass(exc_type, urllib3.exceptions.NewConnectionError):
        return ContreeUrllib3NewConnectionError
    if issubclass(exc_type, urllib3.exceptions.TimeoutError):
        return ContreeUrllib3TimeoutError
    if issubclass(exc_type, urllib3.exceptions.SSLError):
        return ContreeUrllib3SSLError
    if issubclass(exc_type, urllib3.exceptions.DecodeError):
        return ContreeUrllib3DecompressionError
    if issubclass(exc_type, urllib3.exceptions.ProtocolError):
        return ContreeUrllib3ProtocolError
    if issubclass(exc_type, urllib3.exceptions.HTTPError):
        return ContreeUrllib3ConnectionError
    return None


def translate_error(native: BaseException) -> BaseException:
    """Map a native urllib3 exception to its Contree equivalent. Return
    native unchanged when unrecognized."""
    nested_arg = native.args[1] if len(native.args) > 1 else None
    caused_by_reset = (
        isinstance(native.__cause__, ConnectionResetError)
        or isinstance(native.__context__, ConnectionResetError)
        or isinstance(nested_arg, ConnectionResetError)
    )
    if isinstance(native, urllib3.exceptions.ProtocolError) and caused_by_reset:
        wrapped = ContreeUrllib3ConnectionClosedError.wrap(native)
        if wrapped is native:
            return native
        return wrapped
    cls = translate_exc_class(type(native))
    if cls is None:
        return native
    wrapped = cls.wrap(native)
    if wrapped is native:
        return native
    return wrapped


class ContreeClient(base.ContreeSyncClient):
    """Synchronous Contree API client on top of `urllib3.PoolManager`.

    The server compresses every response (including streams); unlike
    requests/httpx/aiohttp, urllib3 does not advertise gzip support on
    its own, so the Accept-Encoding header is set explicitly and the
    responses are transparently decoded (`decode_content=True`).
    """

    log = logger.getChild("urllib3")
    UA_TRANSPORT_LIBRARY = library_version(urllib3)
    retryable_errors = (urllib3.exceptions.HTTPError,)
    nonretryable_errors = (
        urllib3.exceptions.TimeoutError,
        ContreeUrllib3SSLError,
    )

    def __init__(
        self,
        token: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        project: str | None = None,
        timeout: float | None = 300.0,
        retry: RetryPolicy | None = None,
        identity: str | None = None,
        ssl_context: ssl.SSLContext | None = None,
        # adapter-specific, prefixed by adapter name
        urllib3_pool_manager: urllib3.PoolManager | None = None,
    ) -> None:
        super().__init__(
            token,
            base_url=base_url,
            project=project,
            timeout=timeout,
            retry=retry,
            identity=identity,
        )
        if urllib3_pool_manager is not None and ssl_context is not None:
            raise ValueError(
                "ssl_context cannot be combined with urllib3_pool_manager;"
                " configure TLS on the PoolManager itself"
            )
        self.__owns_http = urllib3_pool_manager is None
        self._http = urllib3_pool_manager or urllib3.PoolManager(
            ssl_context=ssl_context
        )

    def _request_headers(self, spec: RequestSpec) -> urllib3.HTTPHeaderDict:
        headers = urllib3.HTTPHeaderDict()
        for name, value in self.build_headers(spec):
            headers.add(name, value)
        if "Accept-Encoding" not in headers:
            headers["Accept-Encoding"] = "gzip"
        return headers

    def request(self, spec: RequestSpec) -> ResponseData:
        url = self.build_url(spec)
        try:
            response = self._http.request(
                spec.method,
                url,
                body=spec.body,
                headers=self._request_headers(spec),
                timeout=self.timeout,
                redirect=False,
                retries=False,
                preload_content=True,
                decode_content=True,
            )
        except urllib3.exceptions.HTTPError as exc:
            translated = translate_error(exc)
            if translated is exc:
                raise
            raise translated from exc
        return ResponseData(
            status=response.status,
            headers={k.lower(): v for k, v in response.headers.items()},
            body=response.data,
        )

    def stream(
        self,
        spec: RequestSpec,
        auto_decompress: bool = True,
    ) -> Iterator[bytes]:
        decode_content = auto_decompress
        url = self.build_url(spec)
        try:
            response = self._http.request(
                spec.method,
                url,
                body=spec.body,
                headers=self._request_headers(spec),
                timeout=urllib3.Timeout(
                    connect=self.timeout,
                    # only SSE may idle (bounded by spec.read_timeout when
                    # a deadline is set); downloads must time out
                    read=spec.read_timeout
                    if spec.accept == "text/event-stream"
                    else self.timeout,
                ),
                redirect=False,
                retries=False,
                preload_content=False,
                decode_content=decode_content,
            )
        except urllib3.exceptions.HTTPError as exc:
            translated = translate_error(exc)
            if translated is exc:
                raise
            raise translated from exc
        try:
            self.log.debug("%s %s -> %d (stream)", spec.method, url, response.status)
            if not 200 <= response.status < 300:
                raise error_for_response(
                    ResponseData(
                        status=response.status,
                        headers={k.lower(): v for k, v in response.headers.items()},
                        body=response.read(decode_content=True),
                    )
                )
            yield from response.stream(CHUNK_SIZE, decode_content=decode_content)
        except urllib3.exceptions.HTTPError as exc:
            translated = translate_error(exc)
            if translated is exc:
                raise
            raise translated from exc
        finally:
            # close before releasing: an aborted stream must not put a
            # half-read connection back into the pool
            response.close()
            response.release_conn()

    def close(self) -> None:
        if self.__owns_http:
            self._http.clear()
