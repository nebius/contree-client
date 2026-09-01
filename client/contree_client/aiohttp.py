"""Contree API client backed by aiohttp."""

from __future__ import annotations

import asyncio
import gzip
import ssl
from collections.abc import AsyncGenerator
from contextlib import suppress
from functools import lru_cache

import aiohttp

from . import base
from .exceptions import (
    ContreeConnectionClosedError,
    ContreeConnectionError,
    ContreeHTTPError,
    ContreeSSLError,
    ContreeStreamError,
    ContreeTimeoutError,
    ContreeTransportError,
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


class ContreeAiohttpConnectionError(
    ContreeConnectionError, aiohttp.ClientConnectionError
):
    """A `ContreeConnectionError` that is also an `aiohttp.ClientConnectionError`."""


class ContreeAiohttpTimeoutError(ContreeTimeoutError, TimeoutError):
    """A `ContreeTimeoutError` that is also a stdlib `TimeoutError`."""


class ContreeAiohttpServerTimeoutError(
    ContreeAiohttpTimeoutError, aiohttp.ServerTimeoutError
):
    """A `ContreeAiohttpTimeoutError` that is also an
    `aiohttp.ServerTimeoutError`.

    This is a subclass of `ContreeAiohttpTimeoutError`, not a sibling.
    Existing `except ContreeAiohttpTimeoutError` call sites still match
    it.
    """


class ContreeAiohttpConnectionClosedError(
    ContreeConnectionClosedError,
    ContreeAiohttpConnectionError,
    aiohttp.ServerDisconnectedError,
):
    """A closed-connection error catchable through both hierarchies."""


class ContreeAiohttpStreamError(ContreeStreamError, aiohttp.ClientPayloadError):
    """A `ContreeProtocolError` that is also an `aiohttp.ClientPayloadError`."""


class ContreeAiohttpSSLError(
    ContreeSSLError, ContreeAiohttpConnectionError, aiohttp.ClientSSLError
):
    """A `ContreeSSLError` that is also an `aiohttp.ClientSSLError`."""


class ContreeAiohttpFingerprintError(
    ContreeSSLError,
    ContreeAiohttpConnectionError,
    aiohttp.ServerFingerprintMismatch,
):
    """A readable TLS certificate-fingerprint mismatch."""

    def __str__(self) -> str:
        return (
            f"TLS fingerprint mismatch for {self.host}:{self.port}: "
            f"expected {self.expected.hex()}, got {self.got.hex()}"
        )


class ContreeAiohttpAPIError(ContreeHTTPError, aiohttp.ClientResponseError):
    """A `ContreeHTTPError` that is also an `aiohttp.ClientResponseError`."""

    @classmethod
    def wrap(cls, original: BaseException) -> BaseException:
        if not isinstance(original, aiohttp.ClientResponseError):
            return original
        try:
            wrapped = cls(
                original.request_info,
                original.history,
                status=original.status,
                message=original.message,
                headers=original.headers,
            )
        except Exception:
            return original
        wrapped.__cause__ = original
        return wrapped


@lru_cache
def translate_exc_class(
    exc_type: type[BaseException],
) -> type[ContreeTransportError] | None:
    """Return the Contree hybrid class for a native exception type.

    Return None when unrecognized. The cache resolves each distinct
    type once, not on every call.

    aiohttp's own hierarchy overlaps in three places. `ServerTimeoutError`
    is also a `ClientConnectionError`. `ClientConnectorSSLError` is also
    a `ClientConnectorError`. `ServerDisconnectedError` is also a
    `ClientConnectionError`. Match each before the generic
    connection-error bucket below it.
    """
    if issubclass(exc_type, aiohttp.ServerTimeoutError):
        return ContreeAiohttpServerTimeoutError
    if issubclass(exc_type, (TimeoutError, asyncio.TimeoutError)):
        return ContreeAiohttpTimeoutError
    if issubclass(exc_type, aiohttp.ServerFingerprintMismatch):
        return ContreeAiohttpFingerprintError
    if issubclass(exc_type, aiohttp.ServerDisconnectedError):
        return ContreeAiohttpConnectionClosedError
    if issubclass(exc_type, aiohttp.ClientSSLError):
        return ContreeAiohttpSSLError
    if issubclass(exc_type, aiohttp.ClientPayloadError):
        return ContreeAiohttpStreamError
    if issubclass(exc_type, aiohttp.ClientResponseError):
        return ContreeAiohttpAPIError
    if issubclass(exc_type, aiohttp.ClientConnectionError):
        return ContreeAiohttpConnectionError
    return None


def translate_error(native: BaseException) -> BaseException:
    """Map a native aiohttp exception to its Contree equivalent. Return
    native unchanged when unrecognized."""
    cls = translate_exc_class(type(native))
    if cls is None:
        return native
    if cls is ContreeAiohttpAPIError:
        # cls comes from translate_exc_class, only for ClientResponseError types
        assert isinstance(native, aiohttp.ClientResponseError)
        wrapped = ContreeAiohttpAPIError.wrap(native)
        if not isinstance(wrapped, ContreeAiohttpAPIError):
            return native
        return wrapped
    wrapped = cls.wrap(native)
    if wrapped is native:
        return native
    return wrapped


class ContreeAsyncClient(base.ContreeAsyncClient):
    """Asynchronous Contree API client on top of `aiohttp.ClientSession`.

    The session is created lazily on the first request so the client
    may be constructed outside of a running event loop.
    """

    log = logger.getChild("aiohttp")
    UA_TRANSPORT_LIBRARY = library_version(aiohttp)
    retryable_errors = (
        aiohttp.ClientConnectionError,
        aiohttp.ServerConnectionError,
        aiohttp.ClientPayloadError,
    )
    nonretryable_errors = (
        TimeoutError,
        asyncio.TimeoutError,
        ContreeAiohttpSSLError,
        ContreeAiohttpFingerprintError,
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
        aiohttp_session: aiohttp.ClientSession | None = None,
    ) -> None:
        super().__init__(
            token,
            base_url=base_url,
            project=project,
            timeout=timeout,
            retry=retry,
            identity=identity,
        )
        if aiohttp_session is not None and ssl_context is not None:
            raise ValueError(
                "ssl_context cannot be combined with aiohttp_session;"
                " configure TLS on the session connector itself"
            )
        self.__owns_session = aiohttp_session is None
        self._session = aiohttp_session
        self._ssl_context = ssl_context

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None:
            connector = (
                aiohttp.TCPConnector(ssl=self._ssl_context)
                if self._ssl_context is not None
                else None
            )
            self._session = aiohttp.ClientSession(connector=connector)
        return self._session

    async def open(self) -> None:
        """Create the session eagerly (`async with client:` path)."""
        self._get_session()

    async def request(self, spec: RequestSpec) -> ResponseData:
        url = self.build_url(spec)
        try:
            async with self._get_session().request(
                spec.method,
                url,
                data=spec.body,
                headers=self.build_headers(spec),
                allow_redirects=False,
                timeout=aiohttp.ClientTimeout(total=self.timeout),
            ) as response:
                body = await response.read()
                return ResponseData(
                    status=response.status,
                    headers={k.lower(): v for k, v in response.headers.items()},
                    body=body,
                )
        except (aiohttp.ClientError, TimeoutError, asyncio.TimeoutError) as exc:
            translated = translate_error(exc)
            if translated is exc:
                raise
            raise translated from exc

    async def stream(
        self,
        spec: RequestSpec,
        auto_decompress: bool = True,
    ) -> AsyncGenerator[bytes, None]:
        url = self.build_url(spec)
        try:
            async with self._get_session().request(
                spec.method,
                url,
                data=spec.body,
                headers=self.build_headers(spec),
                allow_redirects=False,
                auto_decompress=auto_decompress,
                timeout=aiohttp.ClientTimeout(
                    total=None,
                    sock_connect=self.timeout,
                    # only SSE may idle (bounded by spec.read_timeout
                    # when a deadline is set); downloads must time out
                    sock_read=(
                        spec.read_timeout
                        if spec.accept == "text/event-stream"
                        else self.timeout
                    ),
                ),
            ) as response:
                self.log.debug(
                    "%s %s -> %d (stream)", spec.method, url, response.status
                )
                if not 200 <= response.status < 300:
                    try:
                        body = await response.read()
                    except self.retryable_errors:
                        # The status is authoritative even when its optional
                        # diagnostic body is interrupted.
                        body = b""
                    # auto_decompress=False applies to the payload only:
                    # the error body must still be decoded, or the parsed
                    # server message is lost
                    encoding = (response.headers.get("Content-Encoding") or "").lower()
                    if not auto_decompress and encoding == "gzip":
                        with suppress(gzip.BadGzipFile, OSError):
                            body = gzip.decompress(body)
                    raise error_for_response(
                        ResponseData(
                            status=response.status,
                            headers={k.lower(): v for k, v in response.headers.items()},
                            body=body,
                        )
                    )
                async for chunk in response.content.iter_chunked(CHUNK_SIZE):
                    yield chunk
        except (aiohttp.ClientError, TimeoutError, asyncio.TimeoutError) as exc:
            translated = translate_error(exc)
            if translated is exc:
                raise
            raise translated from exc

    async def close(self) -> None:
        if (
            self.__owns_session
            and self._session is not None
            and not self._session.closed
        ):
            await self._session.close()
