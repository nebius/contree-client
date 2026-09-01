"""Contree API clients backed by httpx (sync and async)."""

from __future__ import annotations

import ssl
from collections.abc import AsyncGenerator, Iterator
from functools import lru_cache

import httpx

from . import base
from .exceptions import (
    ContreeConnectionClosedError,
    ContreeConnectionError,
    ContreeHTTPError,
    ContreeProtocolError,
    ContreeSSLError,
    ContreeTimeoutError,
    ContreeTransportError,
    DecompressionError,
)
from .runtime import (
    RequestSpec,
    ResponseData,
    RetryPolicy,
    async_request_content,
    error_for_response,
    library_version,
    remaining_timeout,
    request_content,
)
from .spec_info import DEFAULT_BASE_URL
from .types import logger


class ContreeHttpxConnectionError(ContreeConnectionError, httpx.TransportError):
    """A `ContreeConnectionError` that is also an `httpx.TransportError`."""

    @classmethod
    def wrap(cls, original: BaseException) -> BaseException:
        try:
            wrapped = cls(*original.args)
            if isinstance(original, httpx.HTTPError):
                wrapped._request = original._request
        except Exception:
            return original
        wrapped.__cause__ = original
        return wrapped


class ContreeHttpxTimeoutError(ContreeTimeoutError, httpx.TimeoutException):
    """A `ContreeTimeoutError` that is also an `httpx.TimeoutException`."""

    @classmethod
    def wrap(cls, original: BaseException) -> BaseException:
        try:
            wrapped = cls(*original.args)
            if isinstance(original, httpx.HTTPError):
                wrapped._request = original._request
        except Exception:
            return original
        wrapped.__cause__ = original
        return wrapped


class ContreeHttpxSSLError(
    ContreeSSLError, ContreeHttpxConnectionError, httpx.ConnectError
):
    """A `ContreeSSLError` that is also an `httpx.ConnectError`."""


class ContreeHttpxProtocolError(
    ContreeProtocolError, ContreeHttpxConnectionError, httpx.ProtocolError
):
    """A protocol error catchable through both existing hierarchies."""


class ContreeHttpxRemoteProtocolError(
    ContreeConnectionClosedError,
    ContreeHttpxProtocolError,
    httpx.RemoteProtocolError,
):
    """A translated `httpx.RemoteProtocolError`."""


class ContreeHttpxLocalProtocolError(
    ContreeHttpxProtocolError, httpx.LocalProtocolError
):
    """A translated `httpx.LocalProtocolError`."""


class ContreeHttpxDecompressionError(DecompressionError, httpx.DecodingError):
    """A decoding error that remains an `httpx.DecodingError`."""

    @classmethod
    def wrap(cls, original: BaseException) -> BaseException:
        try:
            wrapped = cls(*original.args)
            if isinstance(original, httpx.HTTPError):
                wrapped._request = original._request
        except Exception:
            return original
        wrapped.__cause__ = original
        return wrapped


class ContreeHttpxAPIError(ContreeHTTPError, httpx.HTTPStatusError):
    """A `ContreeHTTPError` that is also an `httpx.HTTPStatusError`."""


@lru_cache
def translate_exc_class(
    exc_type: type[BaseException],
) -> type[ContreeTransportError] | None:
    """Return the Contree hybrid class for a native exception type.

    Return None when unrecognized. The cache resolves each distinct
    type once, not on every call.

    ConnectError, ConnectTimeout, and the protocol-error subclasses all
    share `httpx.TransportError` as a common ancestor. Check it last,
    as the generic fallback.

    This function does not cover two cases. `httpx.ConnectError` needs
    its `__cause__`, not just its type, to pick SSL vs. plain.
    `httpx.HTTPStatusError` needs `request`/`response` to construct.
    `translate_error()` handles both before it calls this cache.
    """
    if issubclass(exc_type, httpx.TimeoutException):
        return ContreeHttpxTimeoutError
    if issubclass(exc_type, httpx.DecodingError):
        return ContreeHttpxDecompressionError
    if issubclass(exc_type, httpx.RemoteProtocolError):
        return ContreeHttpxRemoteProtocolError
    if issubclass(exc_type, httpx.LocalProtocolError):
        return ContreeHttpxLocalProtocolError
    if issubclass(exc_type, httpx.ProtocolError):
        return ContreeHttpxProtocolError
    if issubclass(exc_type, httpx.TransportError):
        return ContreeHttpxConnectionError
    return None


def translate_error(native: BaseException) -> BaseException:
    """Map a native httpx exception to its Contree equivalent. Return
    native unchanged when unrecognized."""
    cause = native.__cause__
    context = native.__context__
    caused_by_ssl = (
        isinstance(cause, ssl.SSLError)
        or isinstance(context, ssl.SSLError)
        or (cause is not None and isinstance(cause.__cause__, ssl.SSLError))
        or (context is not None and isinstance(context.__cause__, ssl.SSLError))
    )
    if isinstance(native, httpx.ConnectError) and caused_by_ssl:
        wrapped = ContreeHttpxSSLError.wrap(native)
        if wrapped is native:
            return native
        return wrapped
    if isinstance(native, httpx.HTTPStatusError):
        wrapped = ContreeHttpxAPIError(
            str(native), request=native.request, response=native.response
        )
        wrapped.status = native.response.status_code
        wrapped.__cause__ = native
        return wrapped
    cls = translate_exc_class(type(native))
    if cls is None:
        return native
    wrapped = cls.wrap(native)
    if wrapped is native:
        return native
    return wrapped


class ContreeClient(base.ContreeSyncClient):
    """Synchronous Contree API client on top of `httpx.Client`."""

    log = logger.getChild("httpx")
    UA_TRANSPORT_LIBRARY = library_version(httpx)
    retryable_errors = (httpx.TransportError,)
    nonretryable_errors = (
        ContreeHttpxSSLError,
        ContreeHttpxLocalProtocolError,
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
        httpx_client: httpx.Client | None = None,
    ) -> None:
        super().__init__(
            token,
            base_url=base_url,
            project=project,
            timeout=timeout,
            retry=retry,
            identity=identity,
        )
        if httpx_client is not None and ssl_context is not None:
            raise ValueError(
                "ssl_context cannot be combined with httpx_client;"
                " pass verify=ssl_context to the httpx.Client itself"
            )
        self.__owns_client = httpx_client is None
        self._client = httpx_client or httpx.Client(
            timeout=timeout,
            verify=ssl_context if ssl_context is not None else True,
        )

    def request(self, spec: RequestSpec) -> ResponseData:
        url = self.build_url(spec)
        timeout = (
            httpx.USE_CLIENT_DEFAULT
            if spec.deadline is None
            else remaining_timeout(spec.deadline, self.timeout)
        )
        try:
            response = self._client.request(
                spec.method,
                url,
                content=request_content(spec.body),
                # httpx wants a Sequence, materialize the iterable
                headers=list(self.build_headers(spec)),
                timeout=timeout,
            )
        except httpx.HTTPError as exc:
            translated = translate_error(exc)
            if translated is exc:
                raise
            raise translated from exc
        data = ResponseData(
            status=response.status_code,
            headers={k.lower(): v for k, v in response.headers.items()},
            body=response.content,
        )
        remaining_timeout(spec.deadline, None)
        return data

    def stream(
        self,
        spec: RequestSpec,
        auto_decompress: bool = True,
    ) -> Iterator[bytes]:
        url = self.build_url(spec)
        timeout = remaining_timeout(spec.deadline, self.timeout)
        read_timeout = remaining_timeout(
            spec.deadline,
            spec.read_timeout if spec.accept == "text/event-stream" else self.timeout,
        )
        try:
            with self._client.stream(
                spec.method,
                url,
                content=request_content(spec.body),
                # httpx wants a Sequence, materialize the iterable
                headers=list(self.build_headers(spec)),
                timeout=httpx.Timeout(
                    timeout,
                    read=read_timeout,
                ),
            ) as response:
                self.log.debug(
                    "%s %s -> %d (stream)",
                    spec.method,
                    url,
                    response.status_code,
                )
                if not 200 <= response.status_code < 300:
                    response.read()
                    raise error_for_response(
                        ResponseData(
                            status=response.status_code,
                            headers={k.lower(): v for k, v in response.headers.items()},
                            body=response.content,
                        )
                    )
                # no chunk_size: httpx's chunker would buffer small
                # SSE frames until it collects chunk_size bytes
                chunks = (
                    response.iter_bytes() if auto_decompress else response.iter_raw()
                )
                for chunk in chunks:
                    remaining_timeout(spec.deadline, None)
                    yield chunk
        except httpx.HTTPError as exc:
            translated = translate_error(exc)
            if translated is exc:
                raise
            raise translated from exc

    def close(self) -> None:
        if self.__owns_client:
            self._client.close()


class ContreeAsyncClient(base.ContreeAsyncClient):
    """Asynchronous Contree API client on top of `httpx.AsyncClient`."""

    log = logger.getChild("httpx")
    UA_TRANSPORT_LIBRARY = library_version(httpx)
    retryable_errors = (httpx.TransportError,)
    nonretryable_errors = (
        ContreeHttpxSSLError,
        ContreeHttpxLocalProtocolError,
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
        httpx_client: httpx.AsyncClient | None = None,
    ) -> None:
        super().__init__(
            token,
            base_url=base_url,
            project=project,
            timeout=timeout,
            retry=retry,
            identity=identity,
        )
        if httpx_client is not None and ssl_context is not None:
            raise ValueError(
                "ssl_context cannot be combined with httpx_client;"
                " pass verify=ssl_context to the httpx.AsyncClient itself"
            )
        self.__owns_client = httpx_client is None
        self._client = httpx_client or httpx.AsyncClient(
            timeout=timeout,
            verify=ssl_context if ssl_context is not None else True,
        )

    async def request(self, spec: RequestSpec) -> ResponseData:
        url = self.build_url(spec)
        timeout = (
            httpx.USE_CLIENT_DEFAULT
            if spec.deadline is None
            else remaining_timeout(spec.deadline, self.timeout)
        )
        try:
            response = await self._client.request(
                spec.method,
                url,
                # a file-like body must become an ASYNC iterator: httpx
                # refuses sync iterables on an AsyncClient
                content=async_request_content(spec.body),
                # httpx wants a Sequence, materialize the iterable
                headers=list(self.build_headers(spec)),
                timeout=timeout,
            )
        except httpx.HTTPError as exc:
            translated = translate_error(exc)
            if translated is exc:
                raise
            raise translated from exc
        data = ResponseData(
            status=response.status_code,
            headers={k.lower(): v for k, v in response.headers.items()},
            body=response.content,
        )
        remaining_timeout(spec.deadline, None)
        return data

    async def stream(
        self,
        spec: RequestSpec,
        auto_decompress: bool = True,
    ) -> AsyncGenerator[bytes, None]:
        url = self.build_url(spec)
        timeout = remaining_timeout(spec.deadline, self.timeout)
        read_timeout = remaining_timeout(
            spec.deadline,
            spec.read_timeout if spec.accept == "text/event-stream" else self.timeout,
        )
        try:
            async with self._client.stream(
                spec.method,
                url,
                # a file-like body must become an ASYNC iterator: httpx
                # refuses sync iterables on an AsyncClient
                content=async_request_content(spec.body),
                # httpx wants a Sequence, materialize the iterable
                headers=list(self.build_headers(spec)),
                timeout=httpx.Timeout(
                    timeout,
                    read=read_timeout,
                ),
            ) as response:
                self.log.debug(
                    "%s %s -> %d (stream)",
                    spec.method,
                    url,
                    response.status_code,
                )
                if not 200 <= response.status_code < 300:
                    await response.aread()
                    raise error_for_response(
                        ResponseData(
                            status=response.status_code,
                            headers={k.lower(): v for k, v in response.headers.items()},
                            body=response.content,
                        )
                    )
                # no chunk_size: httpx's chunker would buffer small
                # SSE frames until it collects chunk_size bytes
                source = (
                    response.aiter_bytes() if auto_decompress else response.aiter_raw()
                )
                async for chunk in source:
                    remaining_timeout(spec.deadline, None)
                    yield chunk
        except httpx.HTTPError as exc:
            translated = translate_error(exc)
            if translated is exc:
                raise
            raise translated from exc

    async def close(self) -> None:
        if self.__owns_client:
            await self._client.aclose()
