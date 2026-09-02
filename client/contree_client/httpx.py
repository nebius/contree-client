"""Contree API clients backed by httpx (sync and async)."""

from __future__ import annotations

import ssl
from collections.abc import AsyncGenerator, Iterator

import httpx

from . import base
from .exceptions import (
    ContreeConnectionError,
    ContreeError,
    ContreeTimeoutError,
    ContreeTransportError,
    DecompressionError,
    _has_retryable_os_error,
    _is_retryable_os_error,
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


def _api_error(exc: httpx.HTTPStatusError) -> ContreeError:
    response = exc.response
    if 200 <= response.status_code < 300:
        return ContreeTransportError(original=exc)
    try:
        body = response.content
    except httpx.StreamError:
        body = str(exc).encode()
    return error_for_response(
        ResponseData(
            status=response.status_code,
            headers={k.lower(): v for k, v in response.headers.items()},
            body=body,
        ),
        original=exc,
    )


def _connection_error(exc: BaseException) -> ContreeConnectionError:
    return ContreeConnectionError(
        original=exc,
        retryable=_has_retryable_os_error(exc),
    )


def _transport_error(exc: BaseException) -> ContreeTransportError:
    return ContreeTransportError(
        original=exc,
        retryable=_has_retryable_os_error(exc),
    )


class ContreeClient(base.ContreeSyncClient):
    """Synchronous Contree API client on top of `httpx.Client`."""

    log = logger.getChild("httpx")
    UA_TRANSPORT_LIBRARY = library_version(httpx)

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
        content = request_content(spec.body)
        headers = list(self.build_headers(spec))
        timeout = (
            httpx.USE_CLIENT_DEFAULT
            if spec.deadline is None
            else remaining_timeout(spec.deadline, self.timeout)
        )
        try:
            response = self._client.request(
                spec.method,
                url,
                content=content,
                # httpx wants a Sequence, materialize the iterable
                headers=headers,
                timeout=timeout,
            )
        except httpx.HTTPStatusError as exc:
            raise _api_error(exc) from exc
        except httpx.TimeoutException as exc:
            raise ContreeTimeoutError(original=exc, retryable=True) from exc
        except httpx.DecodingError as exc:
            raise DecompressionError(original=exc) from exc
        except httpx.ProxyError as exc:
            raise ContreeTransportError(original=exc) from exc
        except httpx.ConnectError as exc:
            raise _connection_error(exc) from exc
        except httpx.TransportError as exc:
            raise _transport_error(exc) from exc
        except (httpx.HTTPError, httpx.StreamError, httpx.InvalidURL) as exc:
            raise ContreeTransportError(original=exc) from exc
        except OSError as exc:
            raise ContreeConnectionError(
                original=exc,
                retryable=_is_retryable_os_error(exc),
            ) from exc
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
        content = request_content(spec.body)
        headers = list(self.build_headers(spec))
        timeout = remaining_timeout(spec.deadline, self.timeout)
        read_timeout = remaining_timeout(
            spec.deadline,
            spec.read_timeout if spec.accept == "text/event-stream" else self.timeout,
        )
        try:
            with self._client.stream(
                spec.method,
                url,
                content=content,
                # httpx wants a Sequence, materialize the iterable
                headers=headers,
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
        except httpx.HTTPStatusError as exc:
            raise _api_error(exc) from exc
        except httpx.TimeoutException as exc:
            raise ContreeTimeoutError(original=exc, retryable=True) from exc
        except httpx.DecodingError as exc:
            raise DecompressionError(original=exc) from exc
        except httpx.ProxyError as exc:
            raise ContreeTransportError(original=exc) from exc
        except httpx.ConnectError as exc:
            raise _connection_error(exc) from exc
        except httpx.RemoteProtocolError as exc:
            # At this boundary the response stream was already open.
            # A retry means SSE resumption, not blind request replay.
            raise ContreeTransportError(original=exc, retryable=True) from exc
        except httpx.TransportError as exc:
            raise _transport_error(exc) from exc
        except (httpx.HTTPError, httpx.StreamError, httpx.InvalidURL) as exc:
            raise ContreeTransportError(original=exc) from exc
        except OSError as exc:
            raise ContreeConnectionError(
                original=exc,
                retryable=_is_retryable_os_error(exc),
            ) from exc

    def close(self) -> None:
        if self.__owns_client:
            self._client.close()


class ContreeAsyncClient(base.ContreeAsyncClient):
    """Asynchronous Contree API client on top of `httpx.AsyncClient`."""

    log = logger.getChild("httpx")
    UA_TRANSPORT_LIBRARY = library_version(httpx)

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
        content = async_request_content(spec.body)
        headers = list(self.build_headers(spec))
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
                content=content,
                # httpx wants a Sequence, materialize the iterable
                headers=headers,
                timeout=timeout,
            )
        except httpx.HTTPStatusError as exc:
            raise _api_error(exc) from exc
        except httpx.TimeoutException as exc:
            raise ContreeTimeoutError(original=exc, retryable=True) from exc
        except httpx.DecodingError as exc:
            raise DecompressionError(original=exc) from exc
        except httpx.ProxyError as exc:
            raise ContreeTransportError(original=exc) from exc
        except httpx.ConnectError as exc:
            raise _connection_error(exc) from exc
        except httpx.TransportError as exc:
            raise _transport_error(exc) from exc
        except (httpx.HTTPError, httpx.StreamError, httpx.InvalidURL) as exc:
            raise ContreeTransportError(original=exc) from exc
        except OSError as exc:
            raise ContreeConnectionError(
                original=exc,
                retryable=_is_retryable_os_error(exc),
            ) from exc
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
        content = async_request_content(spec.body)
        headers = list(self.build_headers(spec))
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
                content=content,
                # httpx wants a Sequence, materialize the iterable
                headers=headers,
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
        except httpx.HTTPStatusError as exc:
            raise _api_error(exc) from exc
        except httpx.TimeoutException as exc:
            raise ContreeTimeoutError(original=exc, retryable=True) from exc
        except httpx.DecodingError as exc:
            raise DecompressionError(original=exc) from exc
        except httpx.ProxyError as exc:
            raise ContreeTransportError(original=exc) from exc
        except httpx.ConnectError as exc:
            raise _connection_error(exc) from exc
        except httpx.RemoteProtocolError as exc:
            # At this boundary the response stream was already open.
            # A retry means SSE resumption, not blind request replay.
            raise ContreeTransportError(original=exc, retryable=True) from exc
        except httpx.TransportError as exc:
            raise _transport_error(exc) from exc
        except (httpx.HTTPError, httpx.StreamError, httpx.InvalidURL) as exc:
            raise ContreeTransportError(original=exc) from exc
        except OSError as exc:
            raise ContreeConnectionError(
                original=exc,
                retryable=_is_retryable_os_error(exc),
            ) from exc

    async def close(self) -> None:
        if self.__owns_client:
            await self._client.aclose()
