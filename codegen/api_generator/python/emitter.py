"""Rendering of the generated Python contree_client package."""

from __future__ import annotations

import importlib.util
import py_compile
import re
import subprocess
import sys
from collections.abc import Iterable
from pathlib import Path

from api_generator.emitter import GENERATED_NOTE, Emitter
from api_generator.ir import (
    INDENT,
    OpDef,
    SpecIR,
    build_docstring,
)

# Files the generator owns inside the otherwise static package.
GENERATED_FILES = (
    "__init__.py",
    "base.py",
    "models.py",
    "operations.py",
    "spec_info.py",
)

EVENT_DATA_BLOCK = '''
EventData = (
    EventDataInit
    | EventDataSpawn
    | EventDataStream
    | EventDataExit
    | EventDataTruncated
    | EventDataSizeCap
    | EventDataNetwork
    | EventDataShutdown
    | EventDataCompletion
)

EVENT_DATA_PARSERS: dict[str, Callable[[dict[str, Any]], EventData]] = {
    "init": EventDataInit.from_dict,
    "spawn": EventDataSpawn.from_dict,
    "stdin": EventDataStream.from_dict,
    "stdout": EventDataStream.from_dict,
    "stderr": EventDataStream.from_dict,
    "exit": EventDataExit.from_dict,
    "truncated": EventDataTruncated.from_dict,
    "size_cap": EventDataSizeCap.from_dict,
    "network": EventDataNetwork.from_dict,
    "shutdown": EventDataShutdown.from_dict,
    "completion": EventDataCompletion.from_dict,
}


def parse_event_data(
    event_type: str,
    data: Any,
) -> EventData | Any:
    """Decode a per-type event payload.

    Unknown event types and payloads that do not match the documented
    schema - a non-mapping body included - are returned as-is instead
    of failing the stream.
    """
    if not isinstance(data, dict):
        return data
    parser = EVENT_DATA_PARSERS.get(event_type)
    if parser is None:
        return data
    try:
        return parser(data)
    except (KeyError, TypeError, ValueError):
        return data


def decode_chunk(data: object) -> bytes:
    """Decode a stdout/stderr event payload to raw bytes.

    Typed payloads delegate to :meth:`EventDataStream.as_bytes`; the
    raw dict a lenient event parse may leave behind is decoded the
    same way, and anything else becomes empty bytes instead of
    failing a live stream.
    """
    if isinstance(data, (EventDataStream, StreamRepr)):
        return data.as_bytes()
    if not isinstance(data, dict):
        return b""
    value = data.get("value", "")
    if not isinstance(value, str) or not value:
        return b""
    if str(data.get("encoding", "ascii")) == "base64":
        with suppress(binascii.Error, ValueError):
            return base64.b64decode(value)
        return b""
    return value.encode("utf-8", errors="replace")


def decode_stream(stream: StreamRepr | dict[str, Any] | None) -> str:
    """Decode a ``StreamRepr`` payload (model or raw dict) to a string."""
    if isinstance(stream, (EventDataStream, StreamRepr)):
        return stream.as_text()
    if not isinstance(stream, dict):
        return ""
    value = stream.get("value", "")
    if not isinstance(value, str) or not value:
        return ""
    if stream.get("encoding") == "base64":
        with suppress(binascii.Error, ValueError):
            return base64.b64decode(value).decode("utf-8", errors="replace")
        return ""
    return value
'''

PARSE_DATETIME_BLOCK = '''
FRACTION_RE = re.compile(r"^(.*T\\d{2}:\\d{2}:\\d{2})\\.(\\d+)(.*)$")


def parse_datetime(value: str) -> datetime:
    """Parse an ISO 8601 timestamp, tolerating Z suffix and nanoseconds."""
    v = value.strip()
    if v.endswith(("Z", "z")):
        v = v[:-1] + "+00:00"
    match = FRACTION_RE.match(v)
    if match:
        # python 3.10 fromisoformat accepts only 3- or 6-digit
        # fractions: trim nanoseconds AND zero-pad short fractions
        fraction = match.group(2)[:6].ljust(6, "0")
        v = f"{match.group(1)}.{fraction}{match.group(3)}"
    return datetime.fromisoformat(v)


def wire_value(value: Any) -> Any:
    """Recursively encode a value to its JSON-compatible wire form.

    datetime/Enum are converted wherever they sit - directly in a
    field or nested inside lists and mappings.
    """
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (list, tuple)):
        return [wire_value(item) for item in value]
    if isinstance(value, dict):
        return {key: wire_value(item) for key, item in value.items()}
    return value


def omitted_dict(items: list[tuple[str, Any]]) -> dict[str, Any]:
    """`dict_factory` for `dataclasses.asdict` used by `to_dict()`.

    Fields left unset (`...`) are omitted entirely so the server-side
    defaults apply, while an explicit None is serialized as JSON null;
    datetime and enum values become JSON-friendly at any nesting depth.
    """
    return {key: wire_value(value) for key, value in items if value is not Ellipsis}


TModel = TypeVar("TModel", bound="ContreeModel")


@dataclass
class ContreeModel:
    """Base model with the default wire (de)serialization.

    `to_dict` serializes via `dataclasses.asdict`, omitting unset
    (`...`) fields while keeping explicit None as JSON null.
    `from_dict` builds the model from `parse_fields`; models whose
    fields need conversion (nested models, datetimes, discriminated
    unions) override `parse_fields` only.
    """

    @classmethod
    def parse_fields(cls, data: dict[str, Any]) -> dict[str, Any]:
        names = {item.name for item in fields(cls)}
        return {key: value for key, value in data.items() if key in names}

    @classmethod
    def from_dict(cls: type[TModel], data: dict[str, Any]) -> TModel:
        return cls(**cls.parse_fields(data))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self, dict_factory=omitted_dict)
'''


def used_names(source: str, names: Iterable[str]) -> list[str]:
    return [
        name
        for name in sorted(set(names))
        if re.search(rf"\b{re.escape(name)}\b", source)
    ]


def import_block(module: str, names: Iterable[str]) -> str:
    joined = "\n".join(f"{INDENT}{name}," for name in names)
    return f"from {module} import (\n{joined}\n)"


# ---------------------------------------------------------------------------
# models.py
# ---------------------------------------------------------------------------


def render_operation_status(ir: SpecIR) -> str:
    members = "\n".join(f'{INDENT}{value} = "{value}"' for value in ir.status_values)
    terminal = ", ".join(
        f"OperationStatus.{value}" for value in ir.terminal_status_values
    )
    active = ", ".join(
        f"OperationStatus.{value}"
        for value in ir.status_values
        if value not in ir.terminal_status_values
    )
    enum_source = (
        "class OperationStatus(str, Enum):\n"
        f'{INDENT}"""Operation lifecycle state."""\n\n'
        f"{members}\n\n"
        f"{INDENT}def __str__(self) -> str:\n"
        f"{INDENT * 2}return self.value\n\n"
        f"{INDENT}def is_terminal(self) -> bool:\n"
        f'{INDENT * 2}"""True for statuses that will never change again."""\n'
        f"{INDENT * 2}return self in TERMINAL_STATUSES\n\n"
        f"{INDENT}@classmethod\n"
        f'{INDENT}def terminal(cls) -> frozenset["OperationStatus"]:\n'
        f'{INDENT * 2}"""Statuses that will never change again."""\n'
        f"{INDENT * 2}return TERMINAL_STATUSES\n\n"
        f"{INDENT}@classmethod\n"
        f'{INDENT}def active(cls) -> frozenset["OperationStatus"]:\n'
        f'{INDENT * 2}"""Statuses of operations that are still in flight."""\n'
        f"{INDENT * 2}return ACTIVE_STATUSES"
    )
    # the trailing comma matters: without it a singleton would be
    # frozenset(str) - a set of the value's CHARACTERS, str-enum
    # oblige; an empty set must render frozenset(), not frozenset((,))
    terminal_literal = f"frozenset(({terminal},))" if terminal else "frozenset()"
    active_literal = f"frozenset(({active},))" if active else "frozenset()"
    sets_source = (
        "# str-enum members compare and hash like their values, so these\n"
        '# also answer membership for plain strings ("SUCCESS" in ...)\n'
        f"TERMINAL_STATUSES = {terminal_literal}\n"
        f"ACTIVE_STATUSES = {active_literal}"
    )
    return f"{enum_source}\n\n\n{sets_source}"


def render_models(ir: SpecIR) -> str:
    event_type_literal = "\n".join(
        f"{INDENT}{value!r}," for value in ir.event_type_values
    )
    imports = "\n".join(
        [
            "import base64",
            "import binascii",
            "import re",
            "from collections.abc import Callable",
            "from contextlib import suppress",
            "from dataclasses import asdict, dataclass, field, fields",
            "from datetime import datetime",
            "from enum import Enum",
            "from types import EllipsisType",
            "from typing import Any, Literal, TypeVar",
        ]
    )
    parts = [
        f'"""Data models for the Contree API.\n\n{GENERATED_NOTE}\n"""',
        "from __future__ import annotations",
        imports,
        PARSE_DATETIME_BLOCK.strip("\n"),
        f"OperationEventType = Literal[\n{event_type_literal}\n]",
        render_operation_status(ir),
    ]
    parts.extend(cls.render() for cls in ir.classes)
    parts.append(EVENT_DATA_BLOCK.strip("\n"))
    return "\n\n\n".join(parts) + "\n"


# ---------------------------------------------------------------------------
# operations.py
# ---------------------------------------------------------------------------


def render_operations(ir: SpecIR) -> str:
    bodies: list[str] = []
    for op in ir.operations:
        bodies.append(op.build_src)
        if op.parse_src:
            bodies.append(op.parse_src)
    source = "\n\n\n".join(bodies)
    model_names = used_names(source, [*ir.class_names, "OperationStatus"])
    imports = "\n".join(
        [
            "import json",
            "from datetime import datetime",
            "from types import EllipsisType",
            "from typing import IO, Any, Literal",
            "",
            import_block(".models", model_names),
            import_block(
                ".runtime",
                [
                    "RequestSpec",
                    "ResponseData",
                    "error_for_response",
                    "format_time_param",
                    "json_array",
                    "json_object",
                    "quote_path",
                ],
            ),
        ]
    )
    parts = [
        (
            '"""Request builders and response parsers for the Contree'
            f' API.\n\n{GENERATED_NOTE}\n"""'
        ),
        "from __future__ import annotations",
        imports,
        source,
    ]
    return "\n\n\n".join(parts) + "\n"


# ---------------------------------------------------------------------------
# base.py
# ---------------------------------------------------------------------------

BASE_HEADER = '''"""Base client classes with the full generated API surface.

{note}
"""

# E501 only: spec-provided docstrings embed markdown tables that
# cannot be wrapped without breaking them; everything else in this
# file obeys the line limit and stays lint-gated
# ruff: noqa: E501

from __future__ import annotations

import asyncio
import logging
import platform
import sys
import time
from abc import ABC, abstractmethod
from collections.abc import AsyncGenerator, Iterable, Iterator
from contextlib import aclosing
from datetime import datetime
from pathlib import Path
from types import EllipsisType, TracebackType
from typing import IO, Any, Literal, TypeVar
from urllib.parse import urlsplit

from . import operations
from .exceptions import (
    ContreeAPIError,
    ContreeError,
    DecompressionError,
    NotFoundError,
    SSEStreamError,
)
{model_imports}
from .profiles import AUTH_TYPE_IAM, Profile, ProfileError, resolve_profile
from .runtime import (
    TIGHT_LOOP_FLOOR,
    BodyFormatter,
    HeaderFormatter,
    RequestSpec,
    ResponseData,
    RetryPolicy,
    SSEParser,
    body_start,
    decode_event_frame,
    encode_query,
    file_sha256,
    is_uuid,
    logger,
    package_version,
    retry_after_delay,
    retry_generator,
    rewind_body,
)
from .spec_info import DEFAULT_BASE_URL

TClient = TypeVar("TClient", bound="ContreeClientBase")
TSyncClient = TypeVar("TSyncClient", bound="ContreeSyncClient")
TAsyncClient = TypeVar("TAsyncClient", bound="ContreeAsyncClient")


class ContreeClientBase:
    """Shared configuration, URL and header building.

    Implementations replace ``log`` with a child logger named after
    the backend; raw request/response logging happens here so every
    transport reports uniformly.
    """

    log: logging.Logger = logger
    # transient transport errors an adapter considers safe to retry;
    # nonretryable_errors carves exceptions (timeouts) back out of it
    retryable_errors: tuple[type[BaseException], ...] = ()
    nonretryable_errors: tuple[type[BaseException], ...] = ()
    # User-Agent product tokens; adapters override UA_TRANSPORT_LIBRARY
    UA_PRODUCT = f"contree-client/{{package_version()}}"
    UA_TRANSPORT_LIBRARY = ""
    UA_PYTHON_VERSION = f"Python/{{'.'.join(map(str, sys.version_info[:3]))}}"
    UA_PLATFORM = platform.platform()

    def __init__(
        self,
        token: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        project: str | None = None,
        timeout: float | None = 300.0,
        retry: RetryPolicy | None = None,
        identity: str | None = None,
    ) -> None:
        # a typo like "htps://" must not silently degrade to plaintext
        # HTTP on port 80 with the bearer token in the clear
        parts = urlsplit(base_url)
        if parts.scheme not in ("http", "https"):
            raise ValueError(
                f"unsupported base_url scheme {{parts.scheme!r}}"
                f" in {{base_url!r}}: use http:// or https://"
            )
        if not parts.hostname:
            raise ValueError(f"base_url has no hostname: {{base_url!r}}")
        self.token = token
        self.base_url = base_url.rstrip("/")
        self.project = project
        self.timeout = timeout
        self.retry = retry
        # an application product token prepended to the User-Agent,
        # e.g. identity="my-app/1.2.3" - the library tokens stay
        self.identity = identity

    @classmethod
    def from_profile(
        cls: type[TClient],
        profile: str | Profile | None = None,
        *,
        config_path: str | Path | None = None,
        **kwargs: Any,
    ) -> TClient:
        """Create a client from a saved Contree profile.

        Resolution order: explicit *profile* argument, then the
        ``CONTREE_PROFILE`` environment variable, then the active
        profile recorded in the config file.
        """
        resolved = (
            profile
            if isinstance(profile, Profile)
            else resolve_profile(profile, path=config_path)
        )
        if resolved.token is None or not resolved.token.strip():
            raise ProfileError(f"profile {{resolved.name!r}} has no token")
        if not resolved.url and resolved.auth_type != AUTH_TYPE_IAM:
            raise ProfileError(f"profile {{resolved.name!r}} has no URL")
        return cls(
            resolved.token,
            base_url=resolved.url or DEFAULT_BASE_URL,
            project=resolved.project,
            **kwargs,
        )

    def build_url(self, spec: RequestSpec) -> str:
        url = f"{{self.base_url}}/v1{{spec.path}}"
        if spec.query:
            url = f"{{url}}?{{encode_query(spec.query)}}"
        return url

    def build_headers(self, spec: RequestSpec) -> Iterable[tuple[str, str]]:
        """Build the request headers as an ordered iterable of pairs.

        Pairs, not a mapping: RFC 9110 allows repeated field names,
        which a dict cannot represent. Transports whose libraries only
        accept mappings collapse it themselves.
        """
        headers = [("Authorization", f"Bearer {{self.token}}")]
        if self.project is not None:
            headers.append(("Project", self.project))
        if spec.content_type is not None:
            headers.append(("Content-Type", spec.content_type))
        if spec.accept is not None:
            headers.append(("Accept", spec.accept))
        headers.extend(spec.headers.items())
        if all(key.lower() != "user-agent" for key, unused in headers):
            headers.append(("User-Agent", self.user_agent()))
        return headers

    def user_agent(self) -> str:
        """Compose the User-Agent from the ``UA_*`` product tokens.

        The caller's ``identity`` (constructor kwarg) leads, so an
        application announces itself without erasing the library and
        transport tokens.
        """
        parts = (
            self.identity or "",
            self.UA_PRODUCT,
            self.UA_TRANSPORT_LIBRARY,
            self.UA_PYTHON_VERSION,
            self.UA_PLATFORM,
        )
        return " ".join(part for part in parts if part)

    def log_request(self, spec: RequestSpec) -> None:
        """Log the raw outgoing request; secrets are redacted."""
        if not self.log.isEnabledFor(logging.DEBUG):
            return
        self.log.debug(
            "%s %s headers=%s body=%s",
            spec.method,
            self.build_url(spec),
            HeaderFormatter(self.build_headers(spec)),
            BodyFormatter(spec.body, spec.content_type or ""),
        )

    def log_response(self, spec: RequestSpec, response: ResponseData) -> None:
        """Log the raw buffered response."""
        if not self.log.isEnabledFor(logging.DEBUG):
            return
        self.log.debug(
            "%s %s -> %d headers=%s body=%s",
            spec.method,
            self.build_url(spec),
            response.status,
            HeaderFormatter(response.headers.items()),
            BodyFormatter(
                response.body,
                response.headers.get("content-type", ""),
            ),
        )
'''

SYNC_CLASS_HEADER = '''class ContreeSyncClient(ContreeClientBase, ABC):
    """Synchronous Contree API client interface.

    Backend implementations only provide :meth:`request`,
    :meth:`stream` and :meth:`close`.
    """

    @abstractmethod
    def request(self, spec: RequestSpec) -> ResponseData:
        """Execute the request and return the buffered response."""

    @abstractmethod
    def stream(
        self,
        spec: RequestSpec,
        auto_decompress: bool = True,
    ) -> Iterator[bytes]:
        """Execute the request and yield response body chunks.

        With ``auto_decompress=False`` the body is yielded exactly as
        served (e.g. the gzip the server always applies is kept).
        """

    @abstractmethod
    def close(self) -> None:
        """Release the underlying transport resources."""

    def open(self) -> None:
        """Eagerly initialize the underlying transport.

        Called by ``__enter__``. Backends that create their resources
        lazily override this; the default is a no-op.
        """

    def __enter__(self: TSyncClient) -> TSyncClient:
        self.open()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def call(self, spec: RequestSpec) -> ResponseData:
        """Execute a buffered request, retrying per the client policy.

        Without a policy this is a transparent single :meth:`request`.
        Streaming requests are never routed here - SSE consumers
        reconnect with ``Last-Event-Id`` instead.
        """
        policy = self.retry
        if policy is None:
            return self.request(spec)
        # a lost response after a non-idempotent request (POST) could
        # mean a second execution server-side: never blind-retry
        # unless the caller explicitly opted into that risk. 425 Too
        # Early and 429 Too Many Requests are the exceptions - the
        # backend's contract guarantees both mean the request was
        # rejected before any processing, so replaying is always safe.
        replay_safe = spec.idempotent or policy.retry_unsafe
        delays = retry_generator(policy.delays)
        # a retry must replay exactly the bytes the first attempt sent
        start = body_start(spec)
        attempts = 0
        while True:
            attempts += 1
            exhausted = (
                policy.max_attempts is not None and attempts >= policy.max_attempts
            )
            try:
                response = self.request(spec)
            except self.retryable_errors as exc:
                unretryable = isinstance(exc, self.nonretryable_errors)
                if not replay_safe or unretryable or exhausted:
                    raise
                delay = next(delays)
                self.log.warning(
                    "network error (%s), retrying in %.1fs...",
                    type(exc).__name__,
                    delay,
                )
                time.sleep(delay)
                rewind_body(spec, start)
                continue
            if not policy.retryable_status(response.status) or exhausted:
                return response
            if not replay_safe and response.status not in (425, 429):
                return response
            retry_after = retry_after_delay(response)
            delay = retry_after if retry_after is not None else next(delays)
            self.log.warning(
                "server answered %d, retrying in %.1fs...",
                response.status,
                delay,
            )
            time.sleep(delay)
            rewind_body(spec, start)

    def operation_terminal(self, operation_id: str) -> bool:
        """Best-effort check that the operation reached a terminal state."""
        try:
            status = self.get_operation_status(operation_id).status
        except (ContreeError, *self.retryable_errors):
            return False
        return not isinstance(status, EllipsisType) and status.is_terminal()

    def wait_operation(
        self,
        operation_id: str,
        *,
        timeout: float | None = None,
    ) -> OperationResponse:
        """Wait until the operation finishes, driven by its event stream.

        Follows the SSE event log (push, no polling) until the
        ``completion`` event, then fetches and returns the terminal
        ``OperationResponse``. Raises :class:`TimeoutError` when
        *timeout* seconds elapse first.
        """
        for event in self.follow_operation_events(operation_id, timeout=timeout):
            self.log.debug("wait_operation: event %s %s", event.id, event.type)
        return self.get_operation_status(operation_id)

    def follow_operation_events(
        self,
        operation_id: str,
        *,
        last_event_id: int | None = None,
        spid: int | None = None,
        since: int | None = None,
        timeout: float | None = None,
    ) -> Iterator[OperationEvent]:
        """Stream operation events with transparent reconnection.

        Wraps :meth:`iter_operation_events` (``follow=True``): network
        drops, in-band SSE error frames and retryable API statuses
        (410/425/5xx) reconnect from the last received event id, so no
        event is delivered twice; before every reconnect the operation
        status is probed and iteration ends if it already became
        terminal. Other API errors (404, 403, ...) propagate.
        Iteration also ends after the ``completion`` event. *timeout*
        bounds the whole wait; it is enforced between events and
        reconnect cycles and raises :class:`TimeoutError`.
        """
        last_id = last_event_id
        delays = retry_generator()
        deadline = None if timeout is None else time.monotonic() + timeout

        def check_deadline() -> None:
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError(
                    f"operation {operation_id} events did not complete"
                    f" within {timeout}s"
                )

        while True:
            check_deadline()
            events_before = last_id
            try:
                for event in self.iter_operation_events(
                    operation_id,
                    follow=True,
                    spid=spid,
                    since=since,
                    last_event_id=last_id,
                    deadline=deadline,
                ):
                    last_id = event.id
                    yield event
                    if event.type == "completion":
                        return
                    check_deadline()
            except (SSEStreamError, DecompressionError) as exc:
                # a truncated gzip SSE stream is a broken stream too:
                # reconnect from the last received event id
                if isinstance(exc, SSEStreamError) and exc.last_event_id is not None:
                    last_id = exc.last_event_id
                self.log.warning("stream error (last_id=%s): %s", last_id, exc)
            except ContreeAPIError as exc:
                retryable = exc.status in (410, 425) or 500 <= exc.status < 600
                if not retryable:
                    raise
                if self.operation_terminal(operation_id):
                    return
                self.log.warning("stream connect failed (%d): %s", exc.status, exc)
                retry_after = exc.retry_after
                delay = retry_after if retry_after is not None else next(delays)
                if deadline is not None:
                    # a Retry-After must not sleep past the caller's deadline
                    delay = min(delay, max(0.0, deadline - time.monotonic()))
                time.sleep(delay)
                continue
            except self.retryable_errors as exc:
                if isinstance(exc, self.nonretryable_errors):
                    raise
                self.log.warning("stream broken (last_id=%s): %s", last_id, exc)
            # the stream ended or broke without a completion frame:
            # the retry must not outlive the operation itself
            if self.operation_terminal(operation_id):
                return
            if last_id == events_before:
                time.sleep(TIGHT_LOOP_FLOOR)

    def resolve_image(self, ref: str) -> str:
        """Resolve an image reference to a UUID.

        Accepts a raw UUID, ``tag:NAME`` or a bare tag name.
        """
        if ref.startswith("tag:"):
            return self.inspect_find_image_by_tag(ref[4:])
        if is_uuid(ref):
            return ref
        return self.inspect_find_image_by_tag(ref)

    def ensure_file(
        self,
        content: bytes | IO[bytes],
        *,
        sha256: str | None = None,
    ) -> FileResponse | File:
        """Upload *content* unless the server already stores it.

        The digest (*sha256* when the caller already knows it,
        computed locally otherwise) is probed via :meth:`get_file`;
        only a miss uploads. Returns the stored file record either way
        (both shapes carry ``uuid``, ``sha256`` and ``size``).
        Non-seekable streams cannot be hashed and rewound, so without
        a caller-provided *sha256* they skip deduplication and upload
        directly.
        """
        digest = sha256 if sha256 is not None else file_sha256(content)
        if digest is None:
            return self.upload_file(content)
        try:
            return self.get_file(digest)
        except NotFoundError:
            return self.upload_file(content)
'''

ASYNC_CLASS_HEADER = '''class ContreeAsyncClient(ContreeClientBase, ABC):
    """Asynchronous Contree API client interface.

    Backend implementations only provide :meth:`request`,
    :meth:`stream` and :meth:`close`.
    """

    @abstractmethod
    async def request(self, spec: RequestSpec) -> ResponseData:
        """Execute the request and return the buffered response."""

    @abstractmethod
    def stream(
        self,
        spec: RequestSpec,
        auto_decompress: bool = True,
    ) -> AsyncGenerator[bytes, None]:
        """Execute the request and yield response body chunks.

        With ``auto_decompress=False`` the body is yielded exactly as
        served (e.g. the gzip the server always applies is kept).
        """

    @abstractmethod
    async def close(self) -> None:
        """Release the underlying transport resources."""

    async def open(self) -> None:
        """Eagerly initialize the underlying transport.

        Called by ``__aenter__`` from inside the running event loop,
        so backends can create loop-bound resources here (for example
        the ``aiohttp.ClientSession``). The default is a no-op.
        """

    async def __aenter__(self: TAsyncClient) -> TAsyncClient:
        await self.open()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.close()

    async def call(self, spec: RequestSpec) -> ResponseData:
        """Execute a buffered request, retrying per the client policy.

        Without a policy this is a transparent single :meth:`request`.
        Streaming requests are never routed here - SSE consumers
        reconnect with ``Last-Event-Id`` instead.
        """
        policy = self.retry
        if policy is None:
            return await self.request(spec)
        # a lost response after a non-idempotent request (POST) could
        # mean a second execution server-side: never blind-retry
        # unless the caller explicitly opted into that risk. 425 Too
        # Early and 429 Too Many Requests are the exceptions - the
        # backend's contract guarantees both mean the request was
        # rejected before any processing, so replaying is always safe.
        replay_safe = spec.idempotent or policy.retry_unsafe
        delays = retry_generator(policy.delays)
        # a retry must replay exactly the bytes the first attempt sent
        start = body_start(spec)
        attempts = 0
        while True:
            attempts += 1
            exhausted = (
                policy.max_attempts is not None and attempts >= policy.max_attempts
            )
            try:
                response = await self.request(spec)
            except self.retryable_errors as exc:
                unretryable = isinstance(exc, self.nonretryable_errors)
                if not replay_safe or unretryable or exhausted:
                    raise
                delay = next(delays)
                self.log.warning(
                    "network error (%s), retrying in %.1fs...",
                    type(exc).__name__,
                    delay,
                )
                await asyncio.sleep(delay)
                rewind_body(spec, start)
                continue
            if not policy.retryable_status(response.status) or exhausted:
                return response
            if not replay_safe and response.status not in (425, 429):
                return response
            retry_after = retry_after_delay(response)
            delay = retry_after if retry_after is not None else next(delays)
            self.log.warning(
                "server answered %d, retrying in %.1fs...",
                response.status,
                delay,
            )
            await asyncio.sleep(delay)
            rewind_body(spec, start)

    async def operation_terminal(self, operation_id: str) -> bool:
        """Best-effort check that the operation reached a terminal state."""
        try:
            status = (await self.get_operation_status(operation_id)).status
        except (ContreeError, *self.retryable_errors):
            return False
        return not isinstance(status, EllipsisType) and status.is_terminal()

    async def wait_operation(
        self,
        operation_id: str,
        *,
        timeout: float | None = None,
    ) -> OperationResponse:
        """Wait until the operation finishes, driven by its event stream.

        Follows the SSE event log (push, no polling) until the
        ``completion`` event, then fetches and returns the terminal
        ``OperationResponse``. Raises :class:`TimeoutError` when
        *timeout* seconds elapse first.
        """
        deadline = None if timeout is None else time.monotonic() + timeout
        async for event in self.follow_operation_events(
            operation_id, timeout=timeout
        ):
            self.log.debug("wait_operation: event %s %s", event.id, event.type)
        if deadline is None:
            return await self.get_operation_status(operation_id)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(
                f"operation {operation_id} did not complete within {timeout}s"
            )
        try:
            return await asyncio.wait_for(
                self.get_operation_status(operation_id),
                timeout=remaining,
            )
        except asyncio.TimeoutError as exc:
            raise TimeoutError(
                f"operation {operation_id} did not complete within {timeout}s"
            ) from exc

    async def follow_operation_events(
        self,
        operation_id: str,
        *,
        last_event_id: int | None = None,
        spid: int | None = None,
        since: int | None = None,
        timeout: float | None = None,
    ) -> AsyncGenerator[OperationEvent, None]:
        """Stream operation events with transparent reconnection.

        Wraps :meth:`iter_operation_events` (``follow=True``): network
        drops, in-band SSE error frames and retryable API statuses
        (410/425/5xx) reconnect from the last received event id, so no
        event is delivered twice; before every reconnect the operation
        status is probed and iteration ends if it already became
        terminal. Other API errors (404, 403, ...) propagate.
        Iteration also ends after the ``completion`` event. *timeout*
        bounds the whole wait; it is enforced between events and
        reconnect cycles and raises :class:`TimeoutError`.
        """
        last_id = last_event_id
        delays = retry_generator()
        deadline = None if timeout is None else time.monotonic() + timeout

        def check_deadline() -> None:
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError(
                    f"operation {operation_id} events did not complete"
                    f" within {timeout}s"
                )

        async def operation_terminal_before_deadline() -> bool:
            if deadline is None:
                return await self.operation_terminal(operation_id)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                check_deadline()
            try:
                return await asyncio.wait_for(
                    self.operation_terminal(operation_id),
                    timeout=remaining,
                )
            except asyncio.TimeoutError:
                check_deadline()
                raise

        while True:
            check_deadline()
            events_before = last_id
            try:
                # aclosing: leaving this scope must close the transport
                # stream even when the caller aborts the iteration
                async with aclosing(
                    self.iter_operation_events(
                        operation_id,
                        follow=True,
                        spid=spid,
                        since=since,
                        last_event_id=last_id,
                        deadline=deadline,
                    )
                ) as source:
                    async for event in source:
                        last_id = event.id
                        yield event
                        if event.type == "completion":
                            return
                        check_deadline()
            except (SSEStreamError, DecompressionError) as exc:
                # a truncated gzip SSE stream is a broken stream too:
                # reconnect from the last received event id
                if isinstance(exc, SSEStreamError) and exc.last_event_id is not None:
                    last_id = exc.last_event_id
                self.log.warning("stream error (last_id=%s): %s", last_id, exc)
            except ContreeAPIError as exc:
                retryable = exc.status in (410, 425) or 500 <= exc.status < 600
                if not retryable:
                    raise
                if await operation_terminal_before_deadline():
                    return
                self.log.warning("stream connect failed (%d): %s", exc.status, exc)
                retry_after = exc.retry_after
                delay = retry_after if retry_after is not None else next(delays)
                if deadline is not None:
                    # a Retry-After must not sleep past the caller's deadline
                    delay = min(delay, max(0.0, deadline - time.monotonic()))
                await asyncio.sleep(delay)
                continue
            except self.retryable_errors as exc:
                if isinstance(exc, self.nonretryable_errors):
                    raise
                self.log.warning("stream broken (last_id=%s): %s", last_id, exc)
            # the stream ended or broke without a completion frame:
            # the retry must not outlive the operation itself
            if await operation_terminal_before_deadline():
                return
            if last_id == events_before:
                delay = TIGHT_LOOP_FLOOR
                if deadline is not None:
                    delay = min(delay, max(0.0, deadline - time.monotonic()))
                await asyncio.sleep(delay)

    async def resolve_image(self, ref: str) -> str:
        """Resolve an image reference to a UUID.

        Accepts a raw UUID, ``tag:NAME`` or a bare tag name.
        """
        if ref.startswith("tag:"):
            return await self.inspect_find_image_by_tag(ref[4:])
        if is_uuid(ref):
            return ref
        return await self.inspect_find_image_by_tag(ref)

    async def ensure_file(
        self,
        content: bytes | IO[bytes],
        *,
        sha256: str | None = None,
    ) -> FileResponse | File:
        """Upload *content* unless the server already stores it.

        The digest (*sha256* when the caller already knows it,
        computed locally in a worker thread otherwise, so a large file
        does not block the event loop) is probed via :meth:`get_file`;
        only a miss uploads. Returns the stored file record either way
        (both shapes carry ``uuid``, ``sha256`` and ``size``).
        Non-seekable streams cannot be hashed and rewound, so without
        a caller-provided *sha256* they skip deduplication and upload
        directly.
        """
        if sha256 is not None:
            digest: str | None = sha256
        else:
            digest = await asyncio.to_thread(file_sha256, content)
        if digest is None:
            return await self.upload_file(content)
        try:
            return await self.get_file(digest)
        except NotFoundError:
            return await self.upload_file(content)
'''


def method_signature(op: OpDef, extra: str = "") -> str:
    signature = op.signature
    if signature:
        return f"self, {signature}"
    return "self"


def op_docstring(
    op: OpDef,
    summary: str | None = None,
    extra_entries: list[tuple[str, str]] | None = None,
) -> list[str]:
    """Docstring for a client method: spec summary, description, Args."""
    entries = [(arg.py_name, arg.doc) for arg in op.args if arg.doc]
    entries.extend(extra_entries or [])
    return build_docstring(
        INDENT,
        summary or op.summary or op.name,
        op.description,
        "Args",
        entries,
    )


def emit_call_method(op: OpDef, async_mode: bool) -> list[str]:
    prefix = "async " if async_mode else ""
    awaited = "await self.call(spec)" if async_mode else "self.call(spec)"
    lines = [
        f"{prefix}def {op.name}({method_signature(op)}) -> {op.return_annotation}:",
    ]
    lines.extend(op_docstring(op))
    lines.append(f"{INDENT}spec = operations.build_{op.name}({op.passthrough})")
    lines.append(f"{INDENT}self.log_request(spec)")
    lines.append(f"{INDENT}response = {awaited}")
    lines.append(f"{INDENT}self.log_response(spec, response)")
    lines.append(f"{INDENT}return operations.parse_{op.name}(response)")
    return lines


def emit_stream_method(op: OpDef, async_mode: bool) -> list[str]:
    prefix = "async " if async_mode else ""
    iterator = "AsyncGenerator[bytes, None]" if async_mode else "Iterator[bytes]"
    lines = [
        f"{prefix}def {op.name}_stream({method_signature(op)}) -> {iterator}:",
    ]
    lines.extend(op_docstring(op, summary=f"Streaming variant of `{op.name}()`."))
    lines.append(f"{INDENT}spec = operations.build_{op.name}({op.passthrough})")
    lines.append(f"{INDENT}self.log_request(spec)")
    if async_mode:
        # aclosing: an early aclose() of this generator must close the
        # transport stream too (async finalization is not deterministic)
        lines.append(f"{INDENT}async with aclosing(self.stream(spec)) as source:")
        lines.append(f"{INDENT * 2}async for chunk in source:")
        body_indent = INDENT * 3
    else:
        lines.append(f"{INDENT}for chunk in self.stream(spec):")
        body_indent = INDENT * 2
    lines.append(f'{body_indent}self.log.debug("stream chunk: %d bytes", len(chunk))')
    lines.append(f"{body_indent}yield chunk")
    return lines


COMPRESSED_ARG_DOC = (
    "disable transparent decompression: the body is yielded exactly"
    " as served - a tar.gz stream when the server compresses the"
    " response, a plain tar otherwise."
)


def emit_stream_only_method(op: OpDef, async_mode: bool) -> list[str]:
    prefix = "async " if async_mode else ""
    iterator = "AsyncGenerator[bytes, None]" if async_mode else "Iterator[bytes]"
    signature = method_signature(op)
    separator = ", " if "*" in signature else ", *, "
    signature = f"{signature}{separator}compressed: bool = False"
    lines = [f"{prefix}def {op.name}({signature}) -> {iterator}:"]
    lines.extend(op_docstring(op, extra_entries=[("compressed", COMPRESSED_ARG_DOC)]))
    lines.append(f"{INDENT}spec = operations.build_{op.name}({op.passthrough})")
    lines.append(f"{INDENT}self.log_request(spec)")
    if async_mode:
        # aclosing: an early aclose() of this generator must close the
        # transport stream too (async finalization is not deterministic)
        lines.append(
            f"{INDENT}async with aclosing("
            "self.stream(spec, auto_decompress=not compressed)"
            ") as source:"
        )
        lines.append(f"{INDENT * 2}async for chunk in source:")
        body_indent = INDENT * 3
    else:
        lines.append(
            f"{INDENT}source = self.stream(spec, auto_decompress=not compressed)"
        )
        lines.append(f"{INDENT}for chunk in source:")
        body_indent = INDENT * 2
    lines.append(f'{body_indent}self.log.debug("stream chunk: %d bytes", len(chunk))')
    lines.append(f"{body_indent}yield chunk")
    return lines


def emit_sse_method(op: OpDef, async_mode: bool) -> list[str]:
    prefix = "async " if async_mode else ""
    iterator = (
        "AsyncGenerator[OperationEvent, None]"
        if async_mode
        else "Iterator[OperationEvent]"
    )
    loop = "async for" if async_mode else "for"
    lines = [
        f"{prefix}def {op.name}("
        f"{method_signature(op)}, deadline: float | None = None"
        f") -> {iterator}:",
    ]
    lines.extend(op_docstring(op))
    lines.append(f"{INDENT}spec = operations.build_{op.name}({op.passthrough})")
    # a deadline (monotonic seconds) bounds the whole subscription:
    # the socket read timeout covers silent gaps, while the per-chunk
    # check below covers streams kept alive by keepalive comments
    lines.append(f"{INDENT}if deadline is not None:")
    lines.append(
        f"{INDENT * 2}spec.read_timeout = max(0.0, deadline - time.monotonic())"
    )
    lines.append(f"{INDENT}self.log_request(spec)")
    lines.append(f"{INDENT}parser = SSEParser()")
    lines.append(f"{INDENT}last_seen = last_event_id")
    if async_mode:
        # aclosing: an early aclose() of this generator must close the
        # transport stream too (async finalization is not deterministic)
        lines.append(f"{INDENT}async with aclosing(self.stream(spec)) as source:")
        lines.append(f"{INDENT * 2}{loop} chunk in source:")
        base = INDENT * 3
    else:
        lines.append(f"{INDENT}{loop} chunk in self.stream(spec):")
        base = INDENT * 2
    lines.append(f"{base}if deadline is not None and time.monotonic() >= deadline:")
    lines.append(
        f'{base + INDENT}raise TimeoutError(f"{op.name} exceeded its deadline")'
    )
    lines.append(f"{base}for frame in parser.feed(chunk):")
    lines.append(
        f"{base + INDENT}event = decode_event_frame(frame, last_event_id=last_seen)"
    )
    # id-only frames carry no payload but DO advance the resume
    # cursor: track it before skipping payload-less frames
    lines.append(f"{base + INDENT}if frame.id is not None:")
    lines.append(f"{base + INDENT * 2}last_seen = frame.id")
    lines.append(f"{base + INDENT}if event is None:")
    lines.append(f"{base + INDENT * 2}continue")
    lines.append(f'{base + INDENT}self.log.debug("sse event: %r", event)')
    lines.append(f"{base + INDENT}yield event")
    return lines


# List operations that also get a lazy pagination iterator:
# operation name -> (iterator name, items accessor, item annotation)
ITER_LIST_OPERATIONS = {
    "list_images": ("iter_images", ".images", "Image"),
    "list_operations": ("iter_operations", "", "OperationSummary"),
    "list_files": ("iter_files", ".files", "File"),
}

PAGE_SIZE_DOC = (
    "how many records to fetch per request (the server caps a page at {maximum})"
)
ITER_LIMIT_DOC = "stop after this many records in total; None iterates everything"


def emit_iter_method(op: OpDef, async_mode: bool) -> list[str]:
    """A lazy pagination iterator over one of the list operations.

    Mirrors the list method's filters (minus limit/offset), fetching
    pages transparently as the caller consumes items.
    """
    iter_name, accessor, item = ITER_LIST_OPERATIONS[op.name]
    maximum = op.page_limit_max or 1000
    filters = [a for a in op.args if a.py_name not in ("limit", "offset")]
    prefix = "async " if async_mode else ""
    awaited = "await " if async_mode else ""
    iterator = f"AsyncGenerator[{item}, None]" if async_mode else f"Iterator[{item}]"
    signature_parts = ["self", "*"]
    signature_parts.extend(
        f"{a.py_name}: {a.annotation} = {a.default}" for a in filters
    )
    signature_parts.append(f"page_size: int = {maximum}")
    signature_parts.append("limit: int | None = None")
    passthrough = ", ".join(
        [
            *(f"{a.py_name}={a.py_name}" for a in filters),
            "limit=size",
            "offset=offset",
        ]
    )
    lines = [
        f"{prefix}def {iter_name}({', '.join(signature_parts)}) -> {iterator}:",
    ]
    entries = [(a.py_name, a.doc) for a in filters if a.doc]
    entries.append(("page_size", PAGE_SIZE_DOC.format(maximum=maximum)))
    entries.append(("limit", ITER_LIMIT_DOC))
    lines.extend(
        build_docstring(
            INDENT,
            f"Iterate over {op.name}() results across pages.",
            "Offset pagination happens transparently as items are"
            " consumed; breaking out of the loop stops fetching. Note"
            " that offset pagination is not a snapshot - records"
            " created or deleted between page fetches may shift, so"
            " items can repeat or be skipped under concurrent"
            " modification.",
            "Args",
            entries,
        )
    )
    lines.extend(
        [
            # the server silently caps pages at its maximum: a larger
            # page_size would make the short-page check end the
            # iteration early and lose the tail
            f"{INDENT}if not 1 <= page_size <= {maximum}:",
            f"{INDENT * 2}raise ValueError(",
            f'{INDENT * 3}"page_size must be between 1 and {maximum}"',
            f"{INDENT * 2})",
            f"{INDENT}fetched = 0",
            f"{INDENT}offset = 0",
            f"{INDENT}while True:",
            f"{INDENT * 2}size = (",
            f"{INDENT * 3}page_size"
            f" if limit is None else min(page_size, limit - fetched)",
            f"{INDENT * 2})",
            f"{INDENT * 2}if size <= 0:",
            f"{INDENT * 3}return",
            f"{INDENT * 2}page = ({awaited}self.{op.name}({passthrough})){accessor}",
            f"{INDENT * 2}if isinstance(page, EllipsisType) or not page:",
            f"{INDENT * 3}return",
            f"{INDENT * 2}for item in page:",
            f"{INDENT * 3}yield item",
            f"{INDENT * 3}fetched += 1",
            f"{INDENT * 3}if limit is not None and fetched >= limit:",
            f"{INDENT * 4}return",
            f"{INDENT * 2}if len(page) < size:",
            f"{INDENT * 3}return",
            f"{INDENT * 2}offset += len(page)",
        ]
    )
    return lines


def emit_api_methods(ir: SpecIR, async_mode: bool) -> list[str]:
    blocks: list[str] = []
    for op in ir.operations:
        if op.kind == "sse":
            lines = emit_sse_method(op, async_mode)
        elif op.kind == "stream":
            lines = emit_stream_only_method(op, async_mode)
        else:
            lines = emit_call_method(op, async_mode)
        blocks.append("\n".join(f"{INDENT}{line}" if line else "" for line in lines))
        if op.stream_variant:
            lines = emit_stream_method(op, async_mode)
            blocks.append(
                "\n".join(f"{INDENT}{line}" if line else "" for line in lines)
            )
        if op.name in ITER_LIST_OPERATIONS:
            lines = emit_iter_method(op, async_mode)
            blocks.append(
                "\n".join(f"{INDENT}{line}" if line else "" for line in lines)
            )
    return blocks


def render_base(ir: SpecIR) -> str:
    sync_methods = emit_api_methods(ir, async_mode=False)
    async_methods = emit_api_methods(ir, async_mode=True)
    sync_source = "\n\n".join([SYNC_CLASS_HEADER.rstrip("\n"), *sync_methods])
    async_source = "\n\n".join([ASYNC_CLASS_HEADER.rstrip("\n"), *async_methods])
    model_names = used_names(
        sync_source,
        [*ir.class_names, "OperationEvent", "OperationStatus"],
    )
    header = BASE_HEADER.format(
        note=GENERATED_NOTE,
        model_imports=import_block(".models", model_names),
    )
    return "\n\n\n".join([header.rstrip("\n"), sync_source, async_source]) + "\n"


# ---------------------------------------------------------------------------
# __init__.py
# ---------------------------------------------------------------------------

INIT_HEADER = '''"""Contree API client, generated from the OpenAPI specification.

Pick a backend module and import its ``ContreeClient``::

    from contree_client.requests import ContreeClient

Synchronous backends export ``ContreeClient``, asynchronous ones
export ``ContreeAsyncClient``: ``contree_client.http`` (stdlib
http.client, no extra dependencies), ``contree_client.urllib3`` and
``contree_client.requests`` (sync), ``contree_client.httpx`` (both)
and ``contree_client.aiohttp`` (async).  All of them share the
interface of :class:`contree_client.types.ContreeSyncClient` or
:class:`contree_client.types.ContreeAsyncClient` - annotate against
those base classes to stay backend-agnostic.

When any installed backend will do, let the package pick one::

    from contree_client.sync import ContreeClient
    from contree_client.asyncio import ContreeAsyncClient

{note}
"""
'''

EXCEPTION_NAMES = [
    "BadRequestError",
    "ConflictError",
    "ContreeAPIError",
    "ContreeError",
    "ForbiddenError",
    "GoneError",
    "NotFoundError",
    "SSEStreamError",
    "ServerError",
    "TooEarlyError",
    "UnauthorizedError",
    "UnprocessableEntityError",
]


def render_init(ir: SpecIR) -> str:
    model_names = sorted(
        [
            *ir.class_names,
            "ACTIVE_STATUSES",
            "ContreeModel",
            "EventData",
            "OperationEventType",
            "OperationStatus",
            "TERMINAL_STATUSES",
            "decode_chunk",
            "decode_stream",
            "parse_datetime",
        ]
    )
    exports = sorted(
        model_names
        + EXCEPTION_NAMES
        + [
            "DEFAULT_BASE_URL",
            "Profile",
            "ProfileError",
            "RequestSpec",
            "ResponseData",
            "RetryPolicy",
            "load_profiles",
            "resolve_profile",
        ]
    )
    all_block = "\n".join(f'{INDENT}"{name}",' for name in exports)
    imports = "\n".join(
        [
            import_block(".exceptions", EXCEPTION_NAMES),
            import_block(".models", model_names),
            import_block(
                ".profiles",
                ["Profile", "ProfileError", "load_profiles", "resolve_profile"],
            ),
            import_block(
                ".runtime",
                ["RequestSpec", "ResponseData", "RetryPolicy"],
            ),
            import_block(".spec_info", ["DEFAULT_BASE_URL"]),
        ]
    )
    parts = [
        INIT_HEADER.format(note=GENERATED_NOTE).rstrip("\n"),
        imports,
        f"__all__ = [\n{all_block}\n]",
    ]
    return "\n\n".join(parts) + "\n"


# ---------------------------------------------------------------------------
# spec_info.py and entry point
# ---------------------------------------------------------------------------


def render_spec_info(ir: SpecIR) -> str:
    # the full spec is embedded as the module docstring inside an rst
    # literal block, so autodoc renders it as highlighted YAML instead
    # of parsing it as reStructuredText; backslashes and triple quotes
    # are escaped to keep the docstring a valid literal
    spec_doc = ir.spec_text.replace("\\", "\\\\").replace('"""', '\\"""')
    indented = "\n".join(
        f"{INDENT}{line}" if line.strip() else "" for line in spec_doc.splitlines()
    )
    return (
        f'"""The OpenAPI specification this package was generated from.\n'
        f"\n{GENERATED_NOTE}\n\n"
        ".. code-block:: yaml\n\n"
        f'{indented}\n"""\n\n'
        # the docstring embeds the raw spec verbatim: its long lines,
        # trailing whitespace and unicode are the upstream document,
        # not our code - suppressed file-wide by necessity
        "# ruff: noqa: E501, W291, W293, RUF002\n\n"
        "from __future__ import annotations\n\n"
        f"DEFAULT_BASE_URL = {ir.default_base_url!r}\n\n"
        "# sha256 of the exact OpenAPI document this package was built"
        " from -\n# the build input provenance\n"
        f"SPEC_SHA256 = {ir.spec_sha256!r}\n"
    )


def run_ruff(paths: list[Path]) -> None:
    """Fix, format and *gate* the generated files.

    Ruff is a build dependency; a missing binary or an unfixable
    finding must fail the generation instead of shipping an
    unvalidated artifact.
    """
    if importlib.util.find_spec("ruff") is None:
        raise RuntimeError(
            "ruff is not installed; it is required to validate generated code"
        )
    files = [str(path) for path in paths]
    ruff = [sys.executable, "-m", "ruff"]
    # format first: long generated lines are wrapped before linting,
    # then the lint pass fixes what formatting does not cover (imports)
    subprocess.run([*ruff, "format", "--quiet", *files], check=True)
    subprocess.run([*ruff, "check", "--fix", "--quiet", *files], check=False)
    subprocess.run([*ruff, "format", "--quiet", *files], check=True)
    # the mandatory gate: anything unfixable stops the build
    subprocess.run([*ruff, "check", "--quiet", *files], check=True)


class PythonEmitter(Emitter):
    """Renders the contree_client package; gated by ruff + py_compile."""

    files = GENERATED_FILES

    def render(self, ir: SpecIR) -> dict[str, str]:
        return {
            "__init__.py": render_init(ir),
            "base.py": render_base(ir),
            "models.py": render_models(ir),
            "operations.py": render_operations(ir),
            "spec_info.py": render_spec_info(ir),
        }

    def validate(self, paths: list[Path]) -> None:
        run_ruff(paths)
        for path in paths:
            py_compile.compile(str(path), doraise=True)


def generate(spec_source: str | Path, package_dir: Path) -> Path:
    """Generate the Python contree_client package."""
    return PythonEmitter().generate(spec_source, package_dir)
