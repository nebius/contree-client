"""Contree API client backed by requests."""

from __future__ import annotations

import ssl
from collections.abc import Iterator
from functools import lru_cache
from typing import Any

import requests
import requests.adapters

from . import base
from .exceptions import (
    ContreeConnectionError,
    ContreeHTTPError,
    ContreeProtocolError,
    ContreeSSLError,
    ContreeTimeoutError,
    ContreeTransportError,
    DecompressionError,
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
        wrapped.__cause__ = original
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
        wrapped.__cause__ = original
        return wrapped


class ContreeRequestsSSLError(
    ContreeSSLError,
    ContreeRequestsConnectionError,
    requests.exceptions.SSLError,
):
    """A `ContreeSSLError` that is also a `requests.exceptions.SSLError`."""


class ContreeRequestsProtocolError(
    ContreeProtocolError, requests.exceptions.ChunkedEncodingError
):
    """A protocol error that remains a requests exception."""

    def __str__(self) -> str:
        original = self.original
        if (
            isinstance(original, requests.exceptions.ChunkedEncodingError)
            and len(original.args) == 1
            and isinstance(original.args[0], BaseException)
        ):
            nested = original.args[0]
            if len(nested.args) > 1 and isinstance(nested.args[1], BaseException):
                summary = str(nested.args[0]).strip()
                detail = str(nested.args[1]).strip()
                if summary and detail:
                    return f"{summary}: {detail}"
        return super().__str__()

    @classmethod
    def wrap(cls, original: BaseException) -> BaseException:
        try:
            wrapped = cls(*original.args)
            if isinstance(original, requests.RequestException):
                wrapped.response = original.response
                wrapped.request = original.request
        except Exception:
            return original
        wrapped.__cause__ = original
        return wrapped


class ContreeRequestsDecompressionError(
    DecompressionError, requests.exceptions.ContentDecodingError
):
    """A decoding error that remains a requests exception."""

    @classmethod
    def wrap(cls, original: BaseException) -> BaseException:
        try:
            wrapped = cls(*original.args)
            if isinstance(original, requests.RequestException):
                wrapped.response = original.response
                wrapped.request = original.request
        except Exception:
            return original
        wrapped.__cause__ = original
        return wrapped


class ContreeRequestsAPIError(ContreeHTTPError, requests.exceptions.HTTPError):
    """A `ContreeHTTPError` that is also a `requests.exceptions.HTTPError`."""

    @classmethod
    def wrap(cls, original: BaseException) -> BaseException:
        if not isinstance(original, requests.exceptions.HTTPError):
            return original
        if original.response is None:
            return original
        try:
            wrapped = cls(*original.args)
            wrapped.response = original.response
            wrapped.request = original.request
        except Exception:
            return original
        wrapped.status = original.response.status_code
        wrapped.__cause__ = original
        return wrapped


@lru_cache
def translate_exc_class(
    exc_type: type[BaseException],
) -> type[ContreeTransportError] | None:
    """Return the Contree hybrid class for a native exception type.

    Return None when unrecognized. The cache resolves each distinct
    type once, not on every call.

    `ConnectTimeout` and `.SSLError` both subclass
    `requests.ConnectionError` in requests' own hierarchy. Check them
    before the generic connection-error bucket.
    """
    if issubclass(exc_type, requests.Timeout):
        return ContreeRequestsTimeoutError
    if issubclass(exc_type, requests.exceptions.SSLError):
        return ContreeRequestsSSLError
    if issubclass(exc_type, requests.exceptions.ContentDecodingError):
        return ContreeRequestsDecompressionError
    if issubclass(exc_type, requests.ConnectionError):
        return ContreeRequestsConnectionError
    if issubclass(exc_type, requests.exceptions.ChunkedEncodingError):
        return ContreeRequestsProtocolError
    if issubclass(exc_type, requests.exceptions.HTTPError):
        return ContreeRequestsAPIError
    return None


def translate_error(native: BaseException) -> BaseException:
    """Map a native requests exception to its Contree equivalent.
    Return native unchanged when unrecognized.

    An `HTTPError` without a `.response` attached never came from the
    real request path and carries no status. Pass it through unwrapped
    instead of returning a hollow `ContreeHTTPError`.
    """
    cls = translate_exc_class(type(native))
    if cls is None:
        return native
    if cls is ContreeRequestsAPIError:
        # cls comes from translate_exc_class, only for HTTPError types
        assert isinstance(native, requests.exceptions.HTTPError)
        response = native.response
        if response is None:
            return native
        wrapped = ContreeRequestsAPIError.wrap(native)
        if not isinstance(wrapped, ContreeRequestsAPIError):
            return native
        return wrapped
    wrapped = cls.wrap(native)
    if wrapped is native:
        return native
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
    nonretryable_errors = (requests.Timeout, ContreeRequestsSSLError)

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
        except requests.exceptions.RequestException as exc:
            translated = translate_error(exc)
            if translated is exc:
                raise
            raise translated from exc
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
        except requests.exceptions.RequestException as exc:
            translated = translate_error(exc)
            if translated is exc:
                raise
            raise translated from exc
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
            except requests.exceptions.RequestException as exc:
                translated = translate_error(exc)
                if translated is exc:
                    raise
                raise translated from exc

    def close(self) -> None:
        if self.__owns_session:
            self._session.close()
