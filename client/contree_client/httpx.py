"""Contree API clients backed by httpx (sync and async)."""

from __future__ import annotations

import ssl
from collections.abc import AsyncGenerator, Iterator

import httpx

from . import base
from .exceptions import APIConnectionError
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
                headers=headers,
                timeout=timeout,
            )
            data = ResponseData(
                status=response.status_code,
                headers={k.lower(): v for k, v in response.headers.items()},
                body=response.content,
            )
            if response.status_code >= 400:
                response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            native_response = exc.response
            try:
                data = ResponseData(
                    status=native_response.status_code,
                    headers={k.lower(): v for k, v in native_response.headers.items()},
                    body=native_response.content,
                )
            except Exception as body_exc:
                raise APIConnectionError(
                    str(body_exc),
                    timed_out=isinstance(body_exc, httpx.TimeoutException),
                ) from body_exc
            if native_response.status_code >= 400:
                raise error_for_response(data.status, data.headers, data.body) from exc
            raise APIConnectionError(str(exc)) from exc
        except Exception as exc:
            raise APIConnectionError(
                str(exc), timed_out=isinstance(exc, httpx.TimeoutException)
            ) from exc
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
        with self._client.stream(
            spec.method,
            url,
            content=request_content(spec.body),
            headers=list(self.build_headers(spec)),
            timeout=httpx.Timeout(timeout, read=read_timeout),
        ) as response:
            self.log.debug(
                "%s %s -> %d (stream)",
                spec.method,
                url,
                response.status_code,
            )
            if response.status_code >= 400:
                response.raise_for_status()
            chunks = response.iter_bytes() if auto_decompress else response.iter_raw()
            for chunk in chunks:
                remaining_timeout(spec.deadline, None)
                yield chunk

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
                headers=headers,
                timeout=timeout,
            )
            data = ResponseData(
                status=response.status_code,
                headers={k.lower(): v for k, v in response.headers.items()},
                body=response.content,
            )
            if response.status_code >= 400:
                response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            native_response = exc.response
            try:
                data = ResponseData(
                    status=native_response.status_code,
                    headers={k.lower(): v for k, v in native_response.headers.items()},
                    body=native_response.content,
                )
            except Exception as body_exc:
                raise APIConnectionError(
                    str(body_exc),
                    timed_out=isinstance(body_exc, httpx.TimeoutException),
                ) from body_exc
            if native_response.status_code >= 400:
                raise error_for_response(data.status, data.headers, data.body) from exc
            raise APIConnectionError(str(exc)) from exc
        except Exception as exc:
            raise APIConnectionError(
                str(exc), timed_out=isinstance(exc, httpx.TimeoutException)
            ) from exc
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
        async with self._client.stream(
            spec.method,
            url,
            content=async_request_content(spec.body),
            headers=list(self.build_headers(spec)),
            timeout=httpx.Timeout(timeout, read=read_timeout),
        ) as response:
            self.log.debug(
                "%s %s -> %d (stream)",
                spec.method,
                url,
                response.status_code,
            )
            if response.status_code >= 400:
                response.raise_for_status()
            source = response.aiter_bytes() if auto_decompress else response.aiter_raw()
            async for chunk in source:
                remaining_timeout(spec.deadline, None)
                yield chunk

    async def close(self) -> None:
        if self.__owns_client:
            await self._client.aclose()
