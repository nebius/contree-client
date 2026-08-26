"""Contree API client backed by requests."""

from __future__ import annotations

import ssl
from collections.abc import Iterator
from typing import Any

import requests
import requests.adapters

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


class ContreeRequestsConnectionError(ContreeConnectionError, requests.ConnectionError):
    """A `ContreeConnectionError` that is also a `requests.ConnectionError`."""

    @classmethod
    def wrap(cls, original: BaseException) -> BaseException:
        try:
            wrapped = cls(*original.args)
            if isinstance(original, requests.RequestException):
                wrapped.response = original.response
                wrapped.request = original.request
        except Exception:
            return original
        return wrapped


class ContreeRequestsTimeoutError(ContreeTimeoutError, requests.Timeout):
    """A `ContreeTimeoutError` that is also a `requests.Timeout`."""

    @classmethod
    def wrap(cls, original: BaseException) -> BaseException:
        try:
            wrapped = cls(*original.args)
            if isinstance(original, requests.RequestException):
                wrapped.response = original.response
                wrapped.request = original.request
        except Exception:
            return original
        return wrapped


class SSLContextAdapter(requests.adapters.HTTPAdapter):
    """An HTTPAdapter that hands a caller-supplied SSLContext down to
    the underlying urllib3 pool - requests has no direct kwarg for it."""

    def __init__(self, ssl_context: ssl.SSLContext, **kwargs: Any) -> None:
        self._ssl_context = ssl_context
        super().__init__(**kwargs)

    def init_poolmanager(self, *args: Any, **kwargs: Any) -> None:
        kwargs["ssl_context"] = self._ssl_context
        super().init_poolmanager(*args, **kwargs)


class ContreeClient(base.ContreeSyncClient):
    """Synchronous Contree API client on top of `requests.Session`."""

    log = logger.getChild("requests")
    UA_TRANSPORT_LIBRARY = library_version(requests)
    retryable_errors = (requests.ConnectionError,)
    nonretryable_errors = (requests.Timeout,)

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
        requests_session: requests.Session | None = None,
    ) -> None:
        super().__init__(
            token,
            base_url=base_url,
            project=project,
            timeout=timeout,
            retry=retry,
            identity=identity,
        )
        if requests_session is not None and ssl_context is not None:
            raise ValueError(
                "ssl_context cannot be combined with requests_session;"
                " mount an SSLContextAdapter on the session itself"
            )
        self.__owns_session = requests_session is None
        self._session = requests_session or requests.Session()
        if ssl_context is not None:
            self._session.mount("https://", SSLContextAdapter(ssl_context))

    def request(self, spec: RequestSpec) -> ResponseData:
        url = self.build_url(spec)
        try:
            response = self._session.request(
                spec.method,
                url,
                data=spec.body,
                # requests only accepts a mapping: duplicates collapse
                headers=dict(self.build_headers(spec)),
                timeout=self.timeout,
                allow_redirects=False,
            )
        except self.nonretryable_errors as exc:
            raise ContreeRequestsTimeoutError.wrap(exc) from exc
        except self.retryable_errors as exc:
            raise ContreeRequestsConnectionError.wrap(exc) from exc
        return ResponseData(
            status=response.status_code,
            headers={k.lower(): v for k, v in response.headers.items()},
            body=response.content,
        )

    def _open_stream(self, spec: RequestSpec) -> requests.Response:
        url = self.build_url(spec)
        # only SSE may idle (bounded by spec.read_timeout when a
        # deadline is set); downloads must time out
        read_timeout = (
            spec.read_timeout if spec.accept == "text/event-stream" else self.timeout
        )
        timeout = None if self.timeout is None else (self.timeout, read_timeout)
        try:
            response = self._session.request(
                spec.method,
                url,
                data=spec.body,
                # requests only accepts a mapping: duplicates collapse
                headers=dict(self.build_headers(spec)),
                timeout=timeout,
                allow_redirects=False,
                stream=True,
            )
        except self.nonretryable_errors as exc:
            raise ContreeRequestsTimeoutError.wrap(exc) from exc
        except self.retryable_errors as exc:
            raise ContreeRequestsConnectionError.wrap(exc) from exc
        self.log.debug("%s %s -> %d (stream)", spec.method, url, response.status_code)
        if not 200 <= response.status_code < 300:
            with response:
                raise error_for_response(
                    ResponseData(
                        status=response.status_code,
                        headers={k.lower(): v for k, v in response.headers.items()},
                        body=response.content,
                    )
                )
        return response

    def stream(
        self,
        spec: RequestSpec,
        auto_decompress: bool = True,
    ) -> Iterator[bytes]:
        response = self._open_stream(spec)
        with response:
            try:
                if auto_decompress:
                    yield from response.iter_content(CHUNK_SIZE)
                else:
                    # iter_content always decodes; go one level down to
                    # the underlying urllib3 response for the wire bytes
                    yield from response.raw.stream(CHUNK_SIZE, decode_content=False)
            except self.nonretryable_errors as exc:
                raise ContreeRequestsTimeoutError.wrap(exc) from exc
            except self.retryable_errors as exc:
                raise ContreeRequestsConnectionError.wrap(exc) from exc

    def close(self) -> None:
        if self.__owns_session:
            self._session.close()
