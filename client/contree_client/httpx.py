"""Contree API clients backed by httpx (sync and async)."""

from __future__ import annotations

import ssl
from collections.abc import AsyncGenerator, Iterator

import httpx

from . import base
from .exceptions import ContreeConnectionError, ContreeTimeoutError
from .runtime import (
    RequestSpec,
    ResponseData,
    RetryPolicy,
    async_request_content,
    error_for_response,
    library_version,
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
        return wrapped


class ContreeClient(base.ContreeSyncClient):
    """Synchronous Contree API client on top of `httpx.Client`."""

    log = logger.getChild("httpx")
    UA_TRANSPORT_LIBRARY = library_version(httpx)
    retryable_errors = (httpx.TransportError,)
    nonretryable_errors = (httpx.TimeoutException,)

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
        try:
            response = self._client.request(
                spec.method,
                url,
                content=request_content(spec.body),
                # httpx wants a Sequence, materialize the iterable
                headers=list(self.build_headers(spec)),
            )
        except self.nonretryable_errors as exc:
            raise ContreeHttpxTimeoutError.wrap(exc) from exc
        except self.retryable_errors as exc:
            raise ContreeHttpxConnectionError.wrap(exc) from exc
        return ResponseData(
            status=response.status_code,
            headers={k.lower(): v for k, v in response.headers.items()},
            body=response.content,
        )

    def stream(
        self,
        spec: RequestSpec,
        auto_decompress: bool = True,
    ) -> Iterator[bytes]:
        url = self.build_url(spec)
        try:
            with self._client.stream(
                spec.method,
                url,
                content=request_content(spec.body),
                # httpx wants a Sequence, materialize the iterable
                headers=list(self.build_headers(spec)),
                timeout=httpx.Timeout(
                    self.timeout,
                    # only SSE may idle (bounded by spec.read_timeout when
                    # a deadline is set); downloads must time out
                    read=spec.read_timeout
                    if spec.accept == "text/event-stream"
                    else self.timeout,
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
                if auto_decompress:
                    yield from response.iter_bytes()
                else:
                    yield from response.iter_raw()
        except self.nonretryable_errors as exc:
            raise ContreeHttpxTimeoutError.wrap(exc) from exc
        except self.retryable_errors as exc:
            raise ContreeHttpxConnectionError.wrap(exc) from exc

    def close(self) -> None:
        if self.__owns_client:
            self._client.close()


class ContreeAsyncClient(base.ContreeAsyncClient):
    """Asynchronous Contree API client on top of `httpx.AsyncClient`."""

    log = logger.getChild("httpx")
    UA_TRANSPORT_LIBRARY = library_version(httpx)
    retryable_errors = (httpx.TransportError,)
    nonretryable_errors = (httpx.TimeoutException,)

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
        try:
            response = await self._client.request(
                spec.method,
                url,
                # a file-like body must become an ASYNC iterator: httpx
                # refuses sync iterables on an AsyncClient
                content=async_request_content(spec.body),
                # httpx wants a Sequence, materialize the iterable
                headers=list(self.build_headers(spec)),
            )
        except self.nonretryable_errors as exc:
            raise ContreeHttpxTimeoutError.wrap(exc) from exc
        except self.retryable_errors as exc:
            raise ContreeHttpxConnectionError.wrap(exc) from exc
        return ResponseData(
            status=response.status_code,
            headers={k.lower(): v for k, v in response.headers.items()},
            body=response.content,
        )

    async def stream(
        self,
        spec: RequestSpec,
        auto_decompress: bool = True,
    ) -> AsyncGenerator[bytes, None]:
        url = self.build_url(spec)
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
                    self.timeout,
                    # only SSE may idle (bounded by spec.read_timeout when
                    # a deadline is set); downloads must time out
                    read=spec.read_timeout
                    if spec.accept == "text/event-stream"
                    else self.timeout,
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
                    yield chunk
        except self.nonretryable_errors as exc:
            raise ContreeHttpxTimeoutError.wrap(exc) from exc
        except self.retryable_errors as exc:
            raise ContreeHttpxConnectionError.wrap(exc) from exc

    async def close(self) -> None:
        if self.__owns_client:
            await self._client.aclose()
