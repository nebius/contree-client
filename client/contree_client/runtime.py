"""Transport-agnostic request/response plumbing and SSE parsing."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import re
import zlib
from collections.abc import AsyncIterator, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from functools import lru_cache
from importlib.metadata import PackageNotFoundError, distribution
from time import monotonic
from typing import IO, Any, cast
from urllib.parse import quote, urlencode

from .exceptions import (
    ERROR_CLASSES,
    APIStatusError,
    ServerError,
)
from .models import OperationEvent

CHUNK_SIZE = 65536

# The package logger: explicitly ERROR, so the library stays silent
# unless the user opts in (see contree_client.types.set_log_level).
logger = logging.getLogger("contree_client")
logger.setLevel(logging.ERROR)

SENSITIVE_HEADERS = frozenset(
    {
        "authorization",
        "proxy-authorization",
        "cookie",
        "set-cookie",
        "x-api-key",
        "x-auth-token",
    }
)

# Same policy the API applies to spawned process env (see the
# EventDataSpawn schema): any key ending with one of these suffixes is
# considered a secret, case-insensitively.
SENSITIVE_KEY_SUFFIXES = (
    "token",
    "secret",
    "password",
    "api_key",
    "credentials",
    "authorization",
)

REQUEST_DEADLINE_MESSAGE = "request deadline exceeded"


def remaining_timeout(
    deadline: float | None,
    maximum: float | None,
) -> float | None:
    """Return the smaller of the remaining deadline and *maximum*.

    Raise ``TimeoutError`` before transport I/O when the absolute
    monotonic deadline has elapsed.
    """
    if deadline is None:
        return maximum
    remaining = deadline - monotonic()
    if remaining <= 0:
        raise TimeoutError(REQUEST_DEADLINE_MESSAGE)
    if maximum is None:
        return remaining
    return min(remaining, maximum)


def redact_json(value: Any) -> Any:
    """Recursively replace values of secret-suffixed keys."""
    if isinstance(value, dict):
        return {
            key: "<redacted>"
            if str(key).lower().endswith(SENSITIVE_KEY_SUFFIXES)
            else redact_json(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_json(item) for item in value]
    return value


class HeaderFormatter:
    """Lazy `%s` argument for logging HTTP headers, redacts secrets."""

    def __init__(self, headers: Iterable[tuple[str, str]]) -> None:
        self.headers = headers

    def __str__(self) -> str:
        redacted = {
            key: "<redacted>" if key.lower() in SENSITIVE_HEADERS else value
            for key, value in self.headers
        }
        return repr(redacted)


class BodyFormatter:
    """Lazy `%s` argument for logging HTTP bodies.

    JSON payloads are parsed and re-rendered with the values of
    secret-suffixed keys structurally redacted; other textual payloads
    are rendered as-is. Everything is truncated at *max_size*, binary
    payloads become a size marker and file-like bodies are never
    consumed.
    """

    def __init__(
        self,
        body: bytes | IO[bytes] | None,
        content_type: str = "",
        max_size: int = 4096,
    ) -> None:
        self.body = body
        self.content_type = content_type
        self.max_size = max_size

    def truncated(self, text: str, total: int) -> str:
        if len(text) > self.max_size:
            return f"{text[: self.max_size]}... <truncated, {total}B total>"
        return text

    def __str__(self) -> str:
        if self.body is None:
            return "<none>"
        if not isinstance(self.body, (bytes, bytearray)):
            return "<stream>"
        data = bytes(self.body)
        if not data:
            return "<empty>"
        # empty content type is treated as JSON: redaction must be the
        # default, not something a missing header can switch off
        if not self.content_type or "json" in self.content_type:
            try:
                payload = json.loads(data)
            except (ValueError, UnicodeDecodeError):
                # potentially credential-bearing: never echo raw bytes
                return f"<unparsable body {len(data)}B>"
            text = json.dumps(redact_json(payload), ensure_ascii=False)
            return self.truncated(text, len(data))
        if "text" in self.content_type:
            try:
                # decode the whole (already buffered) body so the
                # truncation marker actually fires for long payloads
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                return f"<binary {len(data)}B>"
            return self.truncated(text, len(data))
        return f"<binary {len(data)}B Content-Type={self.content_type!r}>"


@dataclass
class RequestSpec:
    """A backend-agnostic description of a single API request."""

    method: str
    path: str
    query: Mapping[str, str | Sequence[str]] = field(default_factory=dict)
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes | IO[bytes] | None = None
    content_type: str | None = None
    accept: str | None = None
    # retry safety: only idempotent requests may be replayed after a
    # lost response (a re-sent POST could spawn a second sandbox)
    idempotent: bool = False
    # Monotonic deadline passed to transport waits and retries. Sync buffered
    # reads can report expiry after the response completes.
    # None uses only the client's configured timeout.
    deadline: float | None = None
    # SSE only: bound the idle gap between reads. None keeps the
    # stream open indefinitely (the server sends keepalives); a
    # deadline-driven follower sets its remaining budget here so a
    # silent stream cannot outlive the caller's timeout
    read_timeout: float | None = None


@dataclass
class ResponseData:
    """A fully buffered response; header names are lower-cased."""

    status: int
    headers: dict[str, str]
    body: bytes


def distribution_version(name: str) -> str | None:
    """The installed distribution version via package metadata.

    Editable installs report ``"editable"`` (their recorded version
    is whatever was current at install time, i.e. a lie); missing
    metadata reports None.
    """
    try:
        dist = distribution(name)
    except PackageNotFoundError:
        return None
    raw = dist.read_text("direct_url.json")
    if raw:
        try:
            if json.loads(raw).get("dir_info", {}).get("editable"):
                return "editable"
        except ValueError:
            pass
    return dist.version


def package_version() -> str:
    return distribution_version("contree-client") or "unknown"


@lru_cache
def library_version(module: Any) -> str:
    """A ``name/version`` User-Agent token for a transport module.

    Version resolution: package metadata (``editable`` for editable
    installs), then the module's ``__version__``, then ``unknown`` -
    building the token must never fail the import.
    """
    name = module.__name__
    module_version = (
        distribution_version(name) or getattr(module, "__version__", None) or "unknown"
    )
    return f"{name}/{module_version}"


UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.ASCII | re.IGNORECASE,
)


def is_uuid(ref: str) -> bool:
    """True if *ref* looks like an image/operation UUID."""
    return UUID_RE.match(ref) is not None


def file_sha256(content: bytes | IO[bytes]) -> str | None:
    """The sha256 hexdigest of an upload payload.

    File-like payloads are hashed from their *current* position - the
    exact bytes a subsequent send would transmit - and the position is
    restored afterwards. A non-seekable stream cannot be re-read, so
    it reports None (the caller skips deduplication).
    """
    if isinstance(content, (bytes, bytearray)):
        return hashlib.sha256(content).hexdigest()
    if not content.seekable():
        return None
    start = content.tell()
    digest = hashlib.sha256()
    while chunk := content.read(CHUNK_SIZE):
        digest.update(chunk)
    content.seek(start)
    return digest.hexdigest()


RETRY_DELAYS = (0.1, 0.2, 0.5, 1.0, 2.0, 5.0)

# floor for reconnect loops that made no forward progress, so a server
# returning immediate empty streams does not spin the client
TIGHT_LOOP_FLOOR = 0.5


def retry_generator(delays: tuple[float, ...] = RETRY_DELAYS) -> Iterator[float]:
    """An endless ladder of backoff delays.

    Every ``next()`` returns a valid sleep time: the ladder is walked
    once and then the tail delay repeats forever, so callers never
    guard against ``StopIteration`` and never do index arithmetic. A
    finite retry budget is bounded externally (an attempt counter).
    """

    def generator() -> Iterator[float]:
        yield from delays
        while True:
            yield delays[-1]

    return generator()


@dataclass(frozen=True)
class RetryPolicy:
    """Opt-in retries for buffered request failures.

    Retries ``APIConnectionError`` and the configured HTTP statuses.
    ``server_errors`` extends this to every 5xx status. SSE consumers
    handle stream reconnection separately.
    """

    # 425 (Too Early) and 429 (Too Many Requests) are a backend
    # contract: both mean the request was rejected before any
    # processing, so replaying them is always safe - even for a POST
    # the caller hasn't opted into unsafe retries for (see `call()`)
    statuses: tuple[int, ...] = (410, 425, 429)
    server_errors: bool = True
    delays: tuple[float, ...] = RETRY_DELAYS
    # finite by default: unbounded retries are an explicit choice
    max_attempts: int | None = 10
    # non-idempotent requests (POST) are replayed only when the caller
    # explicitly accepts the double-execution risk - except 425/429,
    # which are always safe to replay (see `call()`)
    retry_unsafe: bool = False

    def __post_init__(self) -> None:
        if not self.delays:
            raise ValueError("RetryPolicy.delays must not be empty")
        for delay in self.delays:
            if not math.isfinite(delay) or delay < 0:
                raise ValueError(
                    f"RetryPolicy delays must be finite and non-negative, got {delay!r}"
                )
        if self.max_attempts is not None and (
            not isinstance(self.max_attempts, int)
            or isinstance(self.max_attempts, bool)
            or self.max_attempts < 1
        ):
            raise ValueError("RetryPolicy.max_attempts must be an integer >= 1")

    def retryable_status(self, status: int) -> bool:
        if status in self.statuses:
            return True
        return self.server_errors and 500 <= status < 600


def body_start(spec: RequestSpec) -> int | None:
    """The position a file-like body starts sending from.

    Captured before the first attempt so a retry replays exactly the
    same bytes; None for absent/bytes/non-seekable bodies.
    """
    body = spec.body
    if body is None or isinstance(body, (bytes, bytearray)):
        return None
    stream = cast("IO[bytes]", body)
    if not stream.seekable():
        return None
    return stream.tell()


def rewind_body(spec: RequestSpec, start: int | None) -> None:
    """Rewind a file-like request body to *start* before a retry."""
    body = spec.body
    if body is None or isinstance(body, (bytes, bytearray)):
        return
    stream = cast("IO[bytes]", body)
    if start is None or not stream.seekable():
        raise ValueError("cannot retry: streaming body is not seekable")
    stream.seek(start)


def parse_retry_after(value: str | None) -> float | None:
    """Parse a Retry-After header: delta-seconds or an HTTP-date.

    Negative values clamp to 0.0; anything unparsable reports None.
    """
    if value is None:
        return None
    try:
        seconds = float(value)
    except ValueError:
        seconds = None
    if seconds is not None:
        # float() happily parses "inf"/"nan": an infinite Retry-After
        # would make the retry loop sleep forever
        return max(0.0, seconds) if math.isfinite(seconds) else None
    try:
        moment = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return max(0.0, (moment - datetime.now(timezone.utc)).total_seconds())


def retry_after_delay(response: ResponseData) -> float | None:
    """The Retry-After header as seconds, when present and valid."""
    return parse_retry_after(response.headers.get("retry-after"))


def quote_path(value: str | int) -> str:
    return quote(str(value), safe="")


def encode_query(query: Mapping[str, str | Sequence[str]]) -> str:
    return urlencode(query, doseq=True, safe="/", quote_via=quote)


def format_time_param(value: str | int | float | datetime) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def json_body(response: ResponseData) -> Any:
    return json.loads(response.body.decode("utf-8"))


def json_object(response: ResponseData) -> dict[str, Any]:
    data = json_body(response)
    if not isinstance(data, dict):
        raise TypeError(f"expected a JSON object, got {type(data).__name__}")
    return data


def json_array(response: ResponseData) -> list[Any]:
    data = json_body(response)
    if not isinstance(data, list):
        raise TypeError(f"expected a JSON array, got {type(data).__name__}")
    return data


def error_for_response(
    status_code: int,
    headers: Mapping[str, str],
    body: bytes,
) -> APIStatusError:
    """Build the exception matching an error response."""
    error: Any = body.decode("utf-8", "replace")
    traceback: list[str] | None = None
    try:
        payload = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        payload = None
    if isinstance(payload, dict):
        error = payload.get("error", error)
        raw_traceback = payload.get("traceback")
        if isinstance(raw_traceback, list):
            traceback = [str(item) for item in raw_traceback]
    parsed_retry = parse_retry_after(headers.get("retry-after"))
    retry_after = None if parsed_retry is None else int(parsed_retry)
    cls = ERROR_CLASSES.get(status_code)
    if cls is None:
        cls = ServerError if status_code >= 500 else APIStatusError
    return cls(
        status_code,
        error,
        traceback=traceback,
        retry_after=retry_after,
    )


class GzipStreamDecoder:
    """Incremental gzip decoder for streamed response bodies.

    Used by transports without built-in decompression (http.client);
    handles Z_SYNC_FLUSH-style streams, so decoded data comes out as
    soon as the server flushes it.
    """

    def __init__(self) -> None:
        self.state = zlib.decompressobj(16 + zlib.MAX_WBITS)

    def decompress(self, data: bytes) -> bytes:
        return self.state.decompress(data)

    def flush(self) -> bytes:
        tail = self.state.flush()
        # physical EOF must coincide with the gzip trailer: a stream
        # cut mid-flight would otherwise pass as a complete payload
        if not self.state.eof:
            raise EOFError(
                "compressed stream ended before the gzip trailer"
                " - the payload is truncated"
            )
        return tail


class IdentityStreamDecoder:
    """No-op decoder for uncompressed response bodies."""

    def decompress(self, data: bytes) -> bytes:
        return data

    def flush(self) -> bytes:
        return b""


def stream_decoder(
    content_encoding: str | None,
) -> GzipStreamDecoder | IdentityStreamDecoder:
    if content_encoding and content_encoding.strip().lower() == "gzip":
        return GzipStreamDecoder()
    return IdentityStreamDecoder()


def request_content(
    body: bytes | IO[bytes] | None,
) -> bytes | Iterator[bytes] | None:
    """Normalize a request body to something httpx accepts as content."""
    if body is None or isinstance(body, bytes):
        return body
    return iter(lambda: body.read(CHUNK_SIZE), b"")


def async_request_content(
    body: bytes | IO[bytes] | None,
) -> bytes | AsyncIterator[bytes] | None:
    """Normalize a request body for an asynchronous transport.

    A file-like body becomes an async iterator whose reads run in a
    worker thread, so blocking file I/O never stalls the event loop
    (and an async client never receives a sync iterator).
    """
    if body is None or isinstance(body, bytes):
        return body

    async def reader() -> AsyncIterator[bytes]:
        while chunk := await asyncio.to_thread(body.read, CHUNK_SIZE):
            yield chunk

    return reader()


@dataclass
class SSEFrame:
    """One parsed Server-Sent Events frame."""

    id: int | None = None
    event: str | None = None
    data: str = ""


class SSEParser:
    """Incremental sans-io parser for `text/event-stream` bytes.

    Feed raw chunks, get complete frames back. Comment lines
    (`: keepalive`) are discarded per the SSE specification.
    """

    # a single SSE line/frame has no business being this large; the
    # cap keeps a misbehaving peer from growing the buffer unbounded
    MAX_BUFFER = 4 * 1024 * 1024

    def __init__(self) -> None:
        self.buffer = bytearray()
        self.event: str | None = None
        self.event_id: int | None = None
        self.data_lines: list[str] = []
        self.pending_size = 0
        self.dirty = False

    def feed(self, chunk: bytes) -> list[SSEFrame]:
        self.buffer += chunk
        # the cap covers BOTH the unterminated line and the frame
        # accumulated so far: many short data lines must not grow the
        # pending event unboundedly
        if len(self.buffer) + self.pending_size > self.MAX_BUFFER:
            raise ValueError(
                f"SSE frame exceeds {self.MAX_BUFFER} bytes before completion"
            )
        frames: list[SSEFrame] = []
        while True:
            newline = self.buffer.find(b"\n")
            if newline < 0:
                break
            raw = bytes(self.buffer[:newline])
            del self.buffer[: newline + 1]
            line = raw.decode("utf-8", "replace").removesuffix("\r")
            if not line:
                frame = self.flush()
                if frame is not None:
                    frames.append(frame)
                continue
            if line.startswith(":"):
                continue
            name, _, value = line.partition(":")
            value = value.removeprefix(" ")
            self.dirty = True
            if name == "id":
                try:
                    self.event_id = int(value)
                except ValueError:
                    self.event_id = None
            elif name == "event":
                self.event = value
            elif name == "data":
                self.data_lines.append(value)
                self.pending_size += len(value)
        return frames

    def flush(self) -> SSEFrame | None:
        """Return the pending frame, if any, and reset the state."""
        if not self.dirty:
            return None
        frame = SSEFrame(
            id=self.event_id,
            event=self.event,
            data="\n".join(self.data_lines),
        )
        self.event = None
        self.event_id = None
        self.data_lines = []
        self.pending_size = 0
        self.dirty = False
        return frame


def decode_event_frame(
    frame: SSEFrame,
    last_event_id: int | None = None,
) -> OperationEvent | None:
    """Decode an SSE frame into an OperationEvent.

    Return None for frames that carry no event payload.
    """
    if frame.event == "sse_error":
        error = ConnectionError(frame.data)
        error.__dict__["last_event_id"] = last_event_id
        raise error
    if not frame.data:
        return None
    payload = json.loads(frame.data)
    if not isinstance(payload, dict):
        return None
    return OperationEvent.from_dict(payload)
