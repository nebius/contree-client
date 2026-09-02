"""Exception hierarchy for the Contree API client."""

from __future__ import annotations

import errno
import socket
from typing import Any

_RETRYABLE_ERRNOS = frozenset(
    value
    for value in (
        errno.ECONNABORTED,
        errno.ECONNREFUSED,
        errno.ECONNRESET,
        errno.EHOSTUNREACH,
        errno.ENETDOWN,
        errno.ENETRESET,
        errno.ENETUNREACH,
        errno.ENOTCONN,
        errno.EPIPE,
        errno.ETIMEDOUT,
        getattr(errno, "EHOSTDOWN", None),
    )
    if value is not None
)

# Python keeps the native Winsock code in OSError.winerror. The errno value is
# only an approximate POSIX translation, so classify the confirmed transient
# Winsock failures directly as well.
_RETRYABLE_WINERRORS = frozenset(
    {
        10050,  # WSAENETDOWN
        10051,  # WSAENETUNREACH
        10052,  # WSAENETRESET
        10053,  # WSAECONNABORTED
        10054,  # WSAECONNRESET
        10057,  # WSAENOTCONN
        10058,  # WSAESHUTDOWN
        10060,  # WSAETIMEDOUT
        10061,  # WSAECONNREFUSED
        10064,  # WSAEHOSTDOWN
        10065,  # WSAEHOSTUNREACH
        11002,  # WSATRY_AGAIN
    }
)


def _exception_chain(error: BaseException) -> tuple[BaseException, ...]:
    """Return the confirmed cause/context chain without following arguments."""
    chain: list[BaseException] = []
    current: BaseException | None = error
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        chain.append(current)
        seen.add(id(current))
        current = current.__cause__ or current.__context__
    return tuple(chain)


def _is_retryable_os_error(error: OSError) -> bool:
    """True only for explicitly classified transient OS network failures."""
    if isinstance(error, socket.gaierror):
        return error.errno == socket.EAI_AGAIN
    if isinstance(
        error,
        (
            ConnectionAbortedError,
            ConnectionRefusedError,
            ConnectionResetError,
            BrokenPipeError,
            TimeoutError,
        ),
    ):
        return True
    winerror = getattr(error, "winerror", None)
    return error.errno in _RETRYABLE_ERRNOS or winerror in _RETRYABLE_WINERRORS


def _has_retryable_os_error(error: BaseException) -> bool:
    """Find a confirmed transient OSError in an exception cause chain."""
    return any(
        isinstance(current, OSError) and _is_retryable_os_error(current)
        for current in _exception_chain(error)
    )


def _safe_error_text(error: BaseException) -> str:
    """Return native diagnostic text even when its formatter is broken."""
    try:
        return str(error)
    except Exception:
        return type(error).__name__


class ContreeError(Exception):
    """Base class for all contree-client errors."""

    def __init__(
        self,
        *args: object,
        original: BaseException | None = None,
    ) -> None:
        super().__init__(*args)
        self._original = original

    @property
    def original(self) -> BaseException | None:
        """The native exception this was translated from, if any.

        Mirrors `__cause__`, which also drives Python's chained
        traceback rendering.
        """
        return self._original if self._original is not None else self.__cause__


class ContreeTransportError(ContreeError):
    """The transport could not complete the request-response exchange.

    Transport errors are non-retryable unless the adapter classifies the
    specific native failure as transient.
    """

    def __init__(
        self,
        message: str | None = None,
        *,
        original: BaseException | None = None,
        retryable: bool = False,
    ) -> None:
        if message is None:
            message = (
                _safe_error_text(original)
                if original is not None
                else "Transport failed"
            )
        super().__init__(message, original=original)
        self.retryable = retryable

    def __str__(self) -> str:
        """Preserve the native diagnostic text on translated errors."""
        original = self.original
        return _safe_error_text(original) if original is not None else super().__str__()


class ContreeConnectionError(ContreeTransportError):
    """Failed to establish or maintain the connection."""

    def __str__(self) -> str:
        return super().__str__() or "Connection failed"


class ContreeTimeoutError(ContreeTransportError):
    """A backend timeout while executing the request."""

    def __str__(self) -> str:
        """Supply a message when a backend raises a bare timeout."""
        return super().__str__() or "Request timed out"


class DecompressionError(ContreeTransportError):
    """The compressed response body ended prematurely or is corrupt."""

    def __str__(self) -> str:
        return ContreeTransportError.__str__(self) or "Response decompression failed"


class SSEStreamError(ContreeTransportError):
    """The server terminated an SSE stream with an in-band error frame.

    Per the API convention this is the in-band equivalent of a 410:
    wait briefly and reconnect passing `last_event_id` (the id of the
    last successfully received event, None when nothing was received).
    """

    def __init__(
        self,
        message: str,
        *,
        last_event_id: int | None = None,
    ) -> None:
        super().__init__(message)
        self.last_event_id = last_event_id


class ContreeAPIError(ContreeError):
    """An HTTP error response returned by the Contree API."""

    def __init__(
        self,
        status: int,
        error: Any,
        *,
        traceback: list[str] | None = None,
        retry_after: int | None = None,
        original: BaseException | None = None,
    ) -> None:
        super().__init__(f"HTTP {status}: {error}", original=original)
        self.status = status
        self.error = error
        self.traceback = traceback
        self.retry_after = retry_after


class BadRequestError(ContreeAPIError):
    """400 Bad Request - invalid request parameters."""


class UnauthorizedError(ContreeAPIError):
    """401 Unauthorized - invalid or missing authentication credentials."""


class ForbiddenError(ContreeAPIError):
    """403 Forbidden - token does not have sufficient permissions."""


class NotFoundError(ContreeAPIError):
    """404 Not Found - the requested resource does not exist."""


class ConflictError(ContreeAPIError):
    """409 Conflict - the operation cannot be completed due to a conflict."""


class GoneError(ContreeAPIError):
    """410 Gone - retry after the `retry_after` delay."""


class UnprocessableEntityError(ContreeAPIError):
    """422 Unprocessable Entity - the path is not usable inside the image."""


class TooEarlyError(ContreeAPIError):
    """425 Too Early - not ready yet, retry after the `retry_after` delay."""


class ServerError(ContreeAPIError):
    """5xx - server-side error."""


ERROR_CLASSES: dict[int, type[ContreeAPIError]] = {
    400: BadRequestError,
    401: UnauthorizedError,
    403: ForbiddenError,
    404: NotFoundError,
    409: ConflictError,
    410: GoneError,
    422: UnprocessableEntityError,
    425: TooEarlyError,
}
