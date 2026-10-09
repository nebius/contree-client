"""Contree API client backed by aiohttp."""

from __future__ import annotations

import asyncio
import gzip
import ssl
from collections.abc import AsyncGenerator, Iterator
from contextlib import contextmanager
from functools import cached_property

import aiohttp
from aiohttp.http_exceptions import ContentLengthError, TransferEncodingError

from . import base
from .exceptions import APIConnectionError
from .runtime import (
    CHUNK_SIZE,
    RequestSpec,
    ResponseData,
    RetryPolicy,
    error_for_response,
    library_version,
    preserve_response_status,
    remaining_timeout,
)
from .spec_info import DEFAULT_BASE_URL
from .types import logger


@contextmanager
def normalize_errors() -> Iterator[None]:
    """Normalize transport failures for buffered and streaming requests."""
    try:
        yield
    except aiohttp.ClientResponseError as exc:
        headers = {k.lower(): v for k, v in (exc.headers or {}).items()}
        raise error_for_response(exc.status, headers, exc.message.encode()) from exc
    except aiohttp.ClientPayloadError as exc:
        # Corrupt compressed bodies must remain visible and must not be retried.
        if not isinstance(exc.__cause__, (ContentLengthError, TransferEncodingError)):
            raise
        raise APIConnectionError(str(exc)) from exc
    except (aiohttp.ClientConnectionError, asyncio.TimeoutError, TimeoutError) as exc:
        # asyncio.TimeoutError is a separate type on Python 3.10.
        raise APIConnectionError(
            str(exc), timed_out=isinstance(exc, (asyncio.TimeoutError, TimeoutError))
        ) from exc


class ContreeAsyncClient(base.ContreeAsyncClient):
    """Asynchronous Contree API client on top of `aiohttp.ClientSession`.

    The owned session is created lazily inside a running event loop.
    """

    log = logger.getChild("aiohttp")
    UA_TRANSPORT_LIBRARY = library_version(aiohttp)

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

        self.__ssl_context = ssl_context
        self.__session = aiohttp_session
        self.__created_session: aiohttp.ClientSession | None = None

    @cached_property
    def _session(self) -> aiohttp.ClientSession:
        if self.__session is not None:
            return self.__session

        session = aiohttp.ClientSession(
            connector=(
                aiohttp.TCPConnector(ssl=self.__ssl_context)
                if self.__ssl_context is not None
                else aiohttp.TCPConnector()
            ),
            connector_owner=True,
        )
        self.__created_session = session
        return session

    async def request(self, spec: RequestSpec) -> ResponseData:
        url = self.build_url(spec)
        headers = list(self.build_headers(spec))
        timeout = remaining_timeout(spec.deadline, self.timeout)
        client_timeout = (
            aiohttp.ClientTimeout(total=timeout)
            if spec.deadline is None
            else aiohttp.ClientTimeout(total=timeout, ceil_threshold=float("inf"))
        )
        with normalize_errors():
            async with self._session.request(
                spec.method,
                url,
                data=spec.body,
                headers=headers,
                allow_redirects=False,
                timeout=client_timeout,
                raise_for_status=False,
            ) as response:
                with (
                    preserve_response_status(
                        response.status,
                        {k.lower(): v for k, v in response.headers.items()},
                    ),
                    normalize_errors(),
                ):
                    body = await response.read()
                data = ResponseData(
                    status=response.status,
                    headers={k.lower(): v for k, v in response.headers.items()},
                    body=body,
                )
        if data.status >= 400:
            raise error_for_response(data.status, data.headers, data.body)
        remaining_timeout(spec.deadline, None)

        return data

    async def stream(
        self,
        spec: RequestSpec,
        auto_decompress: bool = True,
    ) -> AsyncGenerator[bytes, None]:
        with normalize_errors():
            url = self.build_url(spec)
            headers = list(self.build_headers(spec))
            connect_timeout = remaining_timeout(spec.deadline, self.timeout)
            read_timeout = remaining_timeout(
                spec.deadline,
                spec.read_timeout
                if spec.accept == "text/event-stream"
                else self.timeout,
            )
            if spec.deadline is None:
                client_timeout = aiohttp.ClientTimeout(
                    total=None,
                    sock_connect=connect_timeout,
                    sock_read=read_timeout,
                )
            else:
                client_timeout = aiohttp.ClientTimeout(
                    total=remaining_timeout(spec.deadline, None),
                    sock_connect=connect_timeout,
                    sock_read=read_timeout,
                    ceil_threshold=float("inf"),
                )
            async with self._session.request(
                spec.method,
                url,
                data=spec.body,
                headers=headers,
                allow_redirects=False,
                auto_decompress=auto_decompress,
                timeout=client_timeout,
                raise_for_status=False,
            ) as response:
                self.log.debug(
                    "%s %s -> %d (stream)", spec.method, url, response.status
                )
                if response.status >= 400:
                    with (
                        preserve_response_status(
                            response.status,
                            {k.lower(): v for k, v in response.headers.items()},
                        ),
                        normalize_errors(),
                    ):
                        body = await response.read()
                        if (
                            not auto_decompress
                            and response.headers.get("Content-Encoding", "").lower()
                            == "gzip"
                        ):
                            body = gzip.decompress(body)
                        raise error_for_response(
                            response.status,
                            {k.lower(): v for k, v in response.headers.items()},
                            body,
                        )
                async for chunk in response.content.iter_chunked(CHUNK_SIZE):
                    remaining_timeout(spec.deadline, None)
                    yield chunk

    async def close(self) -> None:
        if self.__created_session is None or self.__created_session.closed:
            return
        await self.__created_session.close()
