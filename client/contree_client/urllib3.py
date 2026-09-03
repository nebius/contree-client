"""Contree API client backed by urllib3."""

from __future__ import annotations

import ssl
from collections.abc import Iterator
from typing import Any

import urllib3

from . import base
from .exceptions import APIConnectionError
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


class ContreeClient(base.ContreeSyncClient):
    """Synchronous Contree API client on top of `urllib3.PoolManager`.

    The server compresses every response (including streams); unlike
    requests/httpx/aiohttp, urllib3 does not advertise gzip support on
    its own, so the Accept-Encoding header is set explicitly and the
    responses are transparently decoded (`decode_content=True`).
    """

    log = logger.getChild("urllib3")
    UA_TRANSPORT_LIBRARY = library_version(urllib3)

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
        headers = self._request_headers(spec)
        pool_options: dict[str, Any] = {}
        if spec.deadline is None:
            timeout: float | urllib3.Timeout | None = self.timeout
        else:
            total_timeout = remaining_timeout(spec.deadline, None)
            timeout = urllib3.Timeout(
                total=total_timeout,
                connect=remaining_timeout(spec.deadline, self.timeout),
                read=remaining_timeout(spec.deadline, self.timeout),
            )
            pool_options["pool_timeout"] = remaining_timeout(spec.deadline, None)
        try:
            response = self._http.request(
                spec.method,
                url,
                body=spec.body,
                headers=headers,
                timeout=timeout,
                redirect=False,
                retries=False,
                preload_content=True,
                decode_content=True,
                **pool_options,
            )
            data = ResponseData(
                status=response.status,
                headers={k.lower(): v for k, v in response.headers.items()},
                body=response.data,
            )
        except Exception as exc:
            raise APIConnectionError(
                str(exc),
                timed_out=isinstance(exc, urllib3.exceptions.TimeoutError),
            ) from exc
        if data.status >= 400:
            raise error_for_response(data.status, data.headers, data.body)
        remaining_timeout(spec.deadline, None)
        return data

    def stream(
        self,
        spec: RequestSpec,
        auto_decompress: bool = True,
    ) -> Iterator[bytes]:
        decode_content = auto_decompress
        url = self.build_url(spec)
        headers = self._request_headers(spec)
        connect_timeout = remaining_timeout(spec.deadline, self.timeout)
        read_timeout = remaining_timeout(
            spec.deadline,
            spec.read_timeout if spec.accept == "text/event-stream" else self.timeout,
        )
        pool_options: dict[str, Any] = {}
        if spec.deadline is None:
            timeout = urllib3.Timeout(
                connect=connect_timeout,
                read=read_timeout,
            )
        else:
            timeout = urllib3.Timeout(
                total=remaining_timeout(spec.deadline, None),
                connect=connect_timeout,
                read=read_timeout,
            )
            pool_options["pool_timeout"] = remaining_timeout(spec.deadline, None)
        response = self._http.request(
            spec.method,
            url,
            body=spec.body,
            headers=headers,
            timeout=timeout,
            redirect=False,
            retries=False,
            preload_content=False,
            decode_content=decode_content,
            **pool_options,
        )
        try:
            self.log.debug("%s %s -> %d (stream)", spec.method, url, response.status)
            if response.status >= 400:
                raise urllib3.exceptions.HTTPError(f"HTTP {response.status}")
            for chunk in response.stream(CHUNK_SIZE, decode_content=decode_content):
                remaining_timeout(spec.deadline, None)
                yield chunk
        finally:
            # close before releasing: an aborted stream must not put a
            # half-read connection back into the pool
            response.close()
            response.release_conn()

    def close(self) -> None:
        if self.__owns_http:
            self._http.clear()
