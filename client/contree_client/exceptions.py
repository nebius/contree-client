"""Exception hierarchy for the Contree API client."""

from __future__ import annotations

from typing import Any


class ContreeError(Exception):
    """Base class for all contree-client errors."""


class DecompressionError(ContreeError):
    """The compressed response body ended prematurely or is corrupt."""


class SSEStreamError(ContreeError):
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
    ) -> None:
        super().__init__(f"HTTP {status}: {error}")
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
