"""Public exceptions raised by the Contree API client."""

from __future__ import annotations

from typing import Any


class ContreeError(Exception):
    """Base class for normalized buffered request errors."""


class APIConnectionError(ContreeError):
    """An adapter could not complete a buffered ``request()`` call.

    ``timed_out`` identifies a timeout without exposing a backend type.
    """

    def __init__(self, message: str, *, timed_out: bool = False) -> None:
        super().__init__(message)
        self.timed_out = timed_out


class APIStatusError(ContreeError):
    """A buffered ``request()`` received HTTP status 400 or greater."""

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


class BadRequestError(APIStatusError):
    """The API returned HTTP 400 Bad Request."""


class AuthenticationError(APIStatusError):
    """The API returned HTTP 401 Unauthorized."""


class PermissionDeniedError(APIStatusError):
    """The API returned HTTP 403 Forbidden."""


class NotFoundError(APIStatusError):
    """The API returned HTTP 404 Not Found."""


class ConflictError(APIStatusError):
    """The API returned HTTP 409 Conflict."""


class GoneError(APIStatusError):
    """The API returned HTTP 410 Gone."""


class UnprocessableEntityError(APIStatusError):
    """The API returned HTTP 422 Unprocessable Entity."""


class TooEarlyError(APIStatusError):
    """The API returned HTTP 425 Too Early."""


class RateLimitError(APIStatusError):
    """The API returned HTTP 429 Too Many Requests."""


class ServerError(APIStatusError):
    """The API returned an HTTP status of 500 or greater."""


ERROR_CLASSES: dict[int, type[APIStatusError]] = {
    400: BadRequestError,
    401: AuthenticationError,
    403: PermissionDeniedError,
    404: NotFoundError,
    409: ConflictError,
    410: GoneError,
    422: UnprocessableEntityError,
    425: TooEarlyError,
    429: RateLimitError,
}
