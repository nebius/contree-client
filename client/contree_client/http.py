"""Contree API client backed by the standard library http.client.

No third-party dependencies. The server compresses every response
(including SSE streams) and http.client neither advertises nor decodes
gzip, so both are handled here: buffered bodies are inflated in one
go, streams are inflated incrementally (Z_SYNC_FLUSH-friendly), and
reading uses `read1()` - a plain `read(n)` would block until `n`
bytes arrive and stall SSE frames.

Buffered requests borrow keepalive connections from a small pool
(saving a TLS handshake per call); each stream owns a dedicated
connection until EOF.
"""

from __future__ import annotations

import collections
import gzip
import http.client
import select
import socket
import ssl
import threading
from collections.abc import Callable, Iterator
from urllib.parse import urlsplit

from . import base
from .exceptions import ContreeConnectionError, ContreeTimeoutError
from .runtime import (
    CHUNK_SIZE,
    RequestSpec,
    ResponseData,
    RetryPolicy,
    body_start,
    error_for_response,
    rewind_body,
    stream_decoder,
)
from .spec_info import DEFAULT_BASE_URL
from .types import logger


class ContreeHttpConnectionError(ContreeConnectionError, OSError):
    """A `ContreeConnectionError` that is also an `OSError`."""


class ContreeHttpTimeoutError(ContreeTimeoutError, TimeoutError):
    """A `ContreeTimeoutError` that is also a stdlib `TimeoutError`."""


# the reused keepalive connection may have been closed by the server
# while it sat in the pool; these surface exactly that and warrant one
# resend on a fresh connection (RemoteDisconnected is a BadStatusLine)
STALE_KEEPALIVE_ERRORS = (
    http.client.BadStatusLine,
    ConnectionResetError,
    BrokenPipeError,
)


def read_response(response: http.client.HTTPResponse) -> ResponseData:
    """Read the whole body, inflating gzip when the server used it.

    Content-Encoding/Content-Length are dropped after inflation so
    nobody downstream attempts a second decode against a stale length.
    """
    headers = {k.lower(): v for k, v in response.getheaders()}
    body = response.read()
    if body and headers.get("content-encoding", "").lower() == "gzip":
        body = gzip.decompress(body)
        headers.pop("content-encoding", None)
        headers.pop("content-length", None)
    return ResponseData(status=response.status, headers=headers, body=body)


def connection_alive(connection: http.client.HTTPConnection) -> bool:
    """True when an idle keepalive connection is still usable.

    An idle HTTP connection must be silent: a readable socket while no
    request is in flight means the server closed it (EOF/close_notify)
    or broke the protocol - either way it must not be handed out. The
    zero-timeout select works for TLS sockets too, where a MSG_PEEK
    recv would not.
    """
    sock = connection.sock
    if sock is None:
        return False
    try:
        readable, _, _ = select.select([sock], [], [], 0)
    except (OSError, ValueError):  # the descriptor is already closed
        return False
    return not readable


class ConnectionPool:
    """A bounded LIFO pool of keepalive http.client connections.

    *maxsize* caps the TOTAL number of live connections, not just the
    idle ones: borrowing takes the most recently used idle connection
    (the warmest, least likely to have been closed by the server),
    dials a new one while under the cap, and otherwise blocks until a
    concurrent caller returns or discards one - concurrency cannot
    stampede the server with extra dials. A Condition rather than a
    Queue: discarding a broken connection frees a slot and must wake a
    blocked borrower, which a plain Queue.get() cannot express.
    """

    def __init__(
        self,
        factory: Callable[[], http.client.HTTPConnection],
        maxsize: int = 5,
    ) -> None:
        if maxsize < 1:
            raise ValueError("maxsize must be at least 1")
        self.factory = factory
        self.maxsize = maxsize
        self.idle: collections.deque[http.client.HTTPConnection] = collections.deque()
        self.condition = threading.Condition()
        self.created = 0

    def acquire(self) -> tuple[http.client.HTTPConnection, bool]:
        """Borrow a connection; True means it was reused (maybe stale)."""
        with self.condition:
            while True:
                while self.idle:
                    connection = self.idle.pop()  # LIFO: warmest first
                    if connection_alive(connection):
                        return connection, True
                    # went stale while idle: drop it pre-flight instead
                    # of paying a doomed send and a resend
                    connection.close()
                    self.created -= 1
                    self.condition.notify()
                if self.created < self.maxsize:
                    self.created += 1
                    break
                # at capacity: wait until a concurrent caller returns a
                # connection or frees a slot by discarding a broken one
                self.condition.wait()
        try:
            return self.factory(), False
        except BaseException:
            with self.condition:
                self.created -= 1
                self.condition.notify()
            raise

    def release(self, connection: http.client.HTTPConnection) -> None:
        """Return a healthy connection for reuse."""
        with self.condition:
            self.idle.append(connection)
            self.condition.notify()

    def discard(self, connection: http.client.HTTPConnection) -> None:
        """Close a broken connection and free its slot."""
        connection.close()
        with self.condition:
            self.created -= 1
            self.condition.notify()

    def close(self) -> None:
        """Close every idle connection and free their slots."""
        with self.condition:
            while self.idle:
                self.idle.pop().close()
                self.created -= 1
            self.condition.notify_all()


class ContreeClient(base.ContreeSyncClient):
    """Synchronous Contree API client on top of `http.client`.

    Buffered requests share a keepalive :class:`ConnectionPool`
    (default 25 connections) - a request re-sent once on a fresh
    connection when the borrowed one went stale server-side. Streams
    hold their socket until fully consumed, so each stream gets a
    dedicated connection closed at its end.
    """

    log = logger.getChild("http")
    UA_TRANSPORT_LIBRARY = "http.client"
    retryable_errors = (
        socket.gaierror,
        ConnectionError,
        http.client.HTTPException,
        OSError,
    )
    # a timeout is user-configured and a malformed URL is permanent;
    # retrying either would just walk the backoff ladder for nothing
    nonretryable_errors = (TimeoutError, http.client.InvalidURL)

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
        http_max_connections: int = 25,
    ) -> None:
        super().__init__(
            token,
            base_url=base_url,
            project=project,
            timeout=timeout,
            retry=retry,
            identity=identity,
        )
        parts = urlsplit(self.base_url)
        self._scheme = parts.scheme or "https"
        self._host = parts.hostname or "localhost"
        self._port = parts.port
        self._ssl_context = ssl_context
        self._pool = ConnectionPool(self._connect, maxsize=http_max_connections)

    def _connect(self) -> http.client.HTTPConnection:
        if self._scheme == "https":
            return http.client.HTTPSConnection(
                self._host,
                self._port,
                timeout=self.timeout,
                context=self._ssl_context,
            )
        return http.client.HTTPConnection(
            self._host,
            self._port,
            timeout=self.timeout,
        )

    def _request_headers(self, spec: RequestSpec) -> dict[str, str]:
        # http.client's request() only accepts a mapping:
        # duplicates collapse
        headers = dict(self.build_headers(spec))
        headers.setdefault("Accept-Encoding", "gzip")
        return headers

    def _request_target(self, spec: RequestSpec) -> str:
        parts = urlsplit(self.build_url(spec))
        if parts.query:
            return f"{parts.path}?{parts.query}"
        return parts.path

    def _send_on(
        self,
        connection: http.client.HTTPConnection,
        spec: RequestSpec,
    ) -> http.client.HTTPResponse:
        connection.request(
            spec.method,
            self._request_target(spec),
            body=spec.body,
            headers=self._request_headers(spec),
        )
        return connection.getresponse()

    def request(self, spec: RequestSpec) -> ResponseData:
        start = body_start(spec)
        while True:
            connection, reused = self._pool.acquire()
            try:
                response = self._send_on(connection, spec)
                break
            except BaseException as exc:
                self._pool.discard(connection)
                if (
                    reused
                    and spec.idempotent
                    and isinstance(exc, STALE_KEEPALIVE_ERRORS)
                ):
                    # the pooled connection went stale while idle:
                    # replay on another one (each round discards a
                    # stale candidate, so the loop terminates - a
                    # freshly dialed connection failure raises).
                    # Only idempotent requests: after a lost response
                    # the server may have executed a POST already, so
                    # a transparent resend could double a side effect
                    self.log.debug("stale pooled connection, resending: %s", exc)
                    rewind_body(spec, start)
                    continue
                if isinstance(exc, self.nonretryable_errors):
                    raise ContreeHttpTimeoutError.wrap(exc) from exc
                if isinstance(exc, self.retryable_errors):
                    raise ContreeHttpConnectionError.wrap(exc) from exc
                raise
        try:
            data = read_response(response)
        except BaseException as exc:
            self._pool.discard(connection)
            if isinstance(exc, self.nonretryable_errors):
                raise ContreeHttpTimeoutError.wrap(exc) from exc
            if isinstance(exc, self.retryable_errors):
                raise ContreeHttpConnectionError.wrap(exc) from exc
            raise
        # the body is fully drained: the connection is reusable unless
        # the server asked to close it (`Connection: close` sets
        # will_close) or the underlying socket is already gone
        if response.will_close or connection.sock is None:
            self._pool.discard(connection)
        else:
            self._pool.release(connection)
        return data

    def stream(
        self,
        spec: RequestSpec,
        auto_decompress: bool = True,
    ) -> Iterator[bytes]:
        # a stream owns its socket until EOF: dedicated connection
        connection = self._connect()
        try:
            response = self._send_on(connection, spec)
        except BaseException as exc:
            connection.close()
            if isinstance(exc, self.nonretryable_errors):
                raise ContreeHttpTimeoutError.wrap(exc) from exc
            if isinstance(exc, self.retryable_errors):
                raise ContreeHttpConnectionError.wrap(exc) from exc
            raise
        try:
            self.log.debug(
                "%s %s -> %d (stream)",
                spec.method,
                self.build_url(spec),
                response.status,
            )
            if not 200 <= response.status < 300:
                raise error_for_response(read_response(response))
            # the connect timeout has done its job; only an SSE stream
            # (follow=1) may legitimately stay idle longer than it -
            # a download that stops sending bytes must time out, and a
            # deadline-driven follower bounds SSE via spec.read_timeout
            if spec.accept == "text/event-stream" and connection.sock is not None:
                connection.sock.settimeout(spec.read_timeout)
            decoder = stream_decoder(
                response.getheader("Content-Encoding") if auto_decompress else None
            )
            while True:
                raw = response.read1(CHUNK_SIZE)
                if not raw:
                    tail = decoder.flush()
                    if tail:
                        yield tail
                    return
                chunk = decoder.decompress(raw)
                if chunk:
                    yield chunk
        except BaseException as exc:
            if isinstance(exc, self.nonretryable_errors):
                raise ContreeHttpTimeoutError.wrap(exc) from exc
            if isinstance(exc, self.retryable_errors):
                raise ContreeHttpConnectionError.wrap(exc) from exc
            raise
        finally:
            connection.close()

    def close(self) -> None:
        """Close every idle pooled connection."""
        self._pool.close()
