"""Contree API client backed by aiohttp."""

from __future__ import annotations

import asyncio
import gzip
import ssl
from collections.abc import AsyncGenerator
from contextlib import suppress

import aiohttp

from . import base
from .exceptions import (
    ContreeConnectionError,
    ContreeError,
    ContreeTimeoutError,
    ContreeTransportError,
    _is_retryable_os_error,
)
from .runtime import (
    CHUNK_SIZE,
    RequestSpec,
    ResponseData,
    RetryPolicy,
    error_for_response,
    library_version,
    remaining_timeout,
)
from .spec_info import DEFAULT_BASE_URL
from .types import logger


def _api_error(exc: aiohttp.ClientResponseError) -> ContreeError:
    if exc.status <= 0 or 200 <= exc.status < 300:
        return ContreeTransportError(original=exc)
    headers = (
        {} if exc.headers is None else {k.lower(): v for k, v in exc.headers.items()}
    )
    return error_for_response(
        ResponseData(
            status=exc.status,
            headers=headers,
            body=exc.message.encode(),
        ),
        original=exc,
    )


def _connector_error(exc: aiohttp.ClientConnectorError) -> ContreeConnectionError:
    return ContreeConnectionError(
        original=exc,
        retryable=_is_retryable_os_error(exc.os_error),
    )


class ContreeAsyncClient(base.ContreeAsyncClient):
    """Asynchronous Contree API client on top of `aiohttp.ClientSession`.

    The session is created lazily on the first request so the client
    may be constructed outside of a running event loop.
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
        headers = list(self.build_headers(spec))
        timeout = remaining_timeout(spec.deadline, self.timeout)
        client_timeout = (
            aiohttp.ClientTimeout(total=timeout)
            if spec.deadline is None
            else aiohttp.ClientTimeout(total=timeout, ceil_threshold=float("inf"))
        )
        try:
            async with self._get_session().request(
                spec.method,
                url,
                data=spec.body,
                headers=headers,
                allow_redirects=False,
                timeout=client_timeout,
            ) as response:
                body = await response.read()
                data = ResponseData(
                    status=response.status,
                    headers={k.lower(): v for k, v in response.headers.items()},
                    body=body,
                )
        except aiohttp.ServerTimeoutError as exc:
            raise ContreeTimeoutError(original=exc, retryable=True) from exc
        except (TimeoutError, asyncio.TimeoutError) as exc:
            raise ContreeTimeoutError(original=exc, retryable=True) from exc
        except aiohttp.ServerFingerprintMismatch as exc:
            raise ContreeConnectionError(original=exc) from exc
        except aiohttp.ClientSSLError as exc:
            raise ContreeConnectionError(original=exc) from exc
        except aiohttp.ClientPayloadError as exc:
            raise ContreeTransportError(original=exc, retryable=True) from exc
        except aiohttp.ClientHttpProxyError as exc:
            raise ContreeTransportError(original=exc) from exc
        except aiohttp.ClientResponseError as exc:
            raise _api_error(exc) from exc
        except aiohttp.ClientProxyConnectionError as exc:
            raise ContreeTransportError(original=exc) from exc
        except aiohttp.ClientConnectorError as exc:
            raise _connector_error(exc) from exc
        except aiohttp.ServerDisconnectedError as exc:
            raise ContreeConnectionError(original=exc, retryable=True) from exc
        except aiohttp.ClientConnectionError as exc:
            retryable = isinstance(exc, OSError) and _is_retryable_os_error(exc)
            raise ContreeConnectionError(original=exc, retryable=retryable) from exc
        except aiohttp.ClientError as exc:
            raise ContreeTransportError(original=exc) from exc
        except ValueError as exc:
            raise ContreeTransportError(original=exc) from exc
        except OSError as exc:
            raise ContreeConnectionError(
                original=exc,
                retryable=_is_retryable_os_error(exc),
            ) from exc
        remaining_timeout(spec.deadline, None)
        return data

    async def stream(
        self,
        spec: RequestSpec,
        auto_decompress: bool = True,
    ) -> AsyncGenerator[bytes, None]:
        url = self.build_url(spec)
        headers = list(self.build_headers(spec))
        connect_timeout = remaining_timeout(spec.deadline, self.timeout)
        read_timeout = remaining_timeout(
            spec.deadline,
            spec.read_timeout if spec.accept == "text/event-stream" else self.timeout,
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
        try:
            async with self._get_session().request(
                spec.method,
                url,
                data=spec.body,
                headers=headers,
                allow_redirects=False,
                auto_decompress=auto_decompress,
                timeout=client_timeout,
            ) as response:
                self.log.debug(
                    "%s %s -> %d (stream)", spec.method, url, response.status
                )
                if not 200 <= response.status < 300:
                    try:
                        body = await response.read()
                    except (
                        aiohttp.ClientConnectionError,
                        aiohttp.ClientPayloadError,
                        TimeoutError,
                    ):
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
                    remaining_timeout(spec.deadline, None)
                    yield chunk
        except aiohttp.ServerTimeoutError as exc:
            raise ContreeTimeoutError(original=exc, retryable=True) from exc
        except (TimeoutError, asyncio.TimeoutError) as exc:
            raise ContreeTimeoutError(original=exc, retryable=True) from exc
        except aiohttp.ServerFingerprintMismatch as exc:
            raise ContreeConnectionError(original=exc) from exc
        except aiohttp.ClientSSLError as exc:
            raise ContreeConnectionError(original=exc) from exc
        except aiohttp.ClientPayloadError as exc:
            raise ContreeTransportError(original=exc, retryable=True) from exc
        except aiohttp.ClientHttpProxyError as exc:
            raise ContreeTransportError(original=exc) from exc
        except aiohttp.ClientResponseError as exc:
            raise _api_error(exc) from exc
        except aiohttp.ClientProxyConnectionError as exc:
            raise ContreeTransportError(original=exc) from exc
        except aiohttp.ClientConnectorError as exc:
            raise _connector_error(exc) from exc
        except aiohttp.ServerDisconnectedError as exc:
            raise ContreeConnectionError(original=exc, retryable=True) from exc
        except aiohttp.ClientConnectionError as exc:
            retryable = isinstance(exc, OSError) and _is_retryable_os_error(exc)
            raise ContreeConnectionError(original=exc, retryable=retryable) from exc
        except aiohttp.ClientError as exc:
            raise ContreeTransportError(original=exc) from exc
        except ValueError as exc:
            raise ContreeTransportError(original=exc) from exc
        except OSError as exc:
            raise ContreeConnectionError(
                original=exc,
                retryable=_is_retryable_os_error(exc),
            ) from exc

    async def close(self) -> None:
        if (
            self.__owns_session
            and self._session is not None
            and not self._session.closed
        ):
            await self._session.close()
