"""Contree API client backed by urllib3."""

from __future__ import annotations

import ssl
from collections.abc import Iterator

import urllib3

from . import base
from .exceptions import ContreeConnectionError, ContreeTimeoutError
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
    nonretryable_errors = (urllib3.exceptions.TimeoutError,)

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
        except urllib3.exceptions.NewConnectionError as exc:
            # urllib3 subclasses this from ConnectTimeoutError; refused/
            # unreachable is not a deadline elapsing
            raise ContreeUrllib3ConnectionError.wrap(exc) from exc
        except self.nonretryable_errors as exc:
            raise ContreeUrllib3TimeoutError.wrap(exc) from exc
        except self.retryable_errors as exc:
            raise ContreeUrllib3ConnectionError.wrap(exc) from exc
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
        except urllib3.exceptions.NewConnectionError as exc:
            # urllib3 subclasses this from ConnectTimeoutError; refused/
            # unreachable is not a deadline elapsing
            raise ContreeUrllib3ConnectionError.wrap(exc) from exc
        except self.nonretryable_errors as exc:
            raise ContreeUrllib3TimeoutError.wrap(exc) from exc
        except self.retryable_errors as exc:
            raise ContreeUrllib3ConnectionError.wrap(exc) from exc
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
        except urllib3.exceptions.NewConnectionError as exc:
            # urllib3 subclasses this from ConnectTimeoutError; refused/
            # unreachable is not a deadline elapsing
            raise ContreeUrllib3ConnectionError.wrap(exc) from exc
        except self.nonretryable_errors as exc:
            raise ContreeUrllib3TimeoutError.wrap(exc) from exc
        except self.retryable_errors as exc:
            raise ContreeUrllib3ConnectionError.wrap(exc) from exc
        finally:
            # close before releasing: an aborted stream must not put a
            # half-read connection back into the pool
            response.close()
            response.release_conn()

    def close(self) -> None:
        if self.__owns_http:
            self._http.clear()
