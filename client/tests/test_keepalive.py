"""Every backend must reuse keepalive connections across requests.

The stdlib ``http`` backend runs its own small LIFO ConnectionPool:
buffered requests borrow (and return) keepalive connections, saving a
TLS handshake per call, while remaining safe under concurrency - each
thread simply borrows a distinct connection.

The generated package is imported lazily (``importlib.import_module``
inside the tests, after the ``generated_package`` fixture ran) so the
module stays collectable when the package is not generated yet.
"""

from __future__ import annotations

import asyncio
import hashlib
import http.client
import importlib
import io
import json
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import ModuleType

import pytest

from tests.conftest import ASYNC_BACKENDS, PROJECT, TOKEN, make_client
from tests.stub_server import (
    CLOSING_OPERATION_UUID,
    DROPPING_OPERATION_UUID,
    FLAKY_OPERATION_UUID,
    IMAGE_UUID,
    StubServer,
)

POOLED_BACKENDS = ("urllib3", "requests", "httpx", "httpx_async", "aiohttp")
CALLS = 3


@pytest.mark.parametrize("backend", POOLED_BACKENDS)
def test_sequential_requests_share_one_connection(
    backend: str,
    generated_package: ModuleType,
    stub_server: StubServer,
) -> None:
    if backend in ASYNC_BACKENDS:

        async def run() -> None:
            async with make_client(backend, stub_server.base_url) as client:
                for _ in range(CALLS):
                    await client.whoami()

        asyncio.run(run())
    else:
        with make_client(backend, stub_server.base_url) as client:
            for _ in range(CALLS):
                client.whoami()

    assert len(stub_server.captured) == CALLS
    assert stub_server.connection_count == 1


def test_stdlib_pool_reuses_one_connection(
    generated_package: ModuleType,
    stub_server: StubServer,
) -> None:
    with make_client("http", stub_server.base_url) as client:
        for _ in range(CALLS):
            client.whoami()
        assert len(client._pool.idle) == 1

    assert stub_server.connection_count == 1
    assert not client._pool.idle  # close() drained the pool


def test_stdlib_pool_survives_stale_idle_connection(
    generated_package: ModuleType,
    stub_server: StubServer,
) -> None:
    """A pooled connection that died between requests is discarded by
    the pre-flight liveness check and replaced with a fresh dial."""
    with make_client("http", stub_server.base_url) as client:
        client.whoami()
        client._pool.idle[-1].close()  # server dropped the keepalive
        assert client.whoami().permissions

    assert stub_server.connection_count == 2


def test_stdlib_pool_concurrent_borrowing(
    generated_package: ModuleType,
    stub_server: StubServer,
) -> None:
    """Concurrent callers borrow distinct connections: no shared-socket
    corruption, the total is hard-capped by the pool size."""
    with (
        make_client("http", stub_server.base_url) as client,
        ThreadPoolExecutor(max_workers=8) as executor,
    ):
        client._pool.maxsize = 3  # tighten the cap below the workers
        results = list(executor.map(lambda _: client.whoami(), range(24)))

    assert len(results) == 24
    assert all(r.permissions for r in results)
    # the cap bounds dialing even with more concurrent workers
    assert stub_server.connection_count <= 3


def test_stdlib_pool_drops_connection_on_server_close(
    generated_package: ModuleType,
    stub_server: StubServer,
) -> None:
    """`Connection: close` (and a dead underlying socket) must discard
    the connection instead of returning it to the pool."""
    with make_client("http", stub_server.base_url) as client:
        client.whoami()  # dial + pool one connection
        client.get_operation_status(CLOSING_OPERATION_UUID)
        # the closed connection did not go back into the pool
        assert not client._pool.idle
        assert client._pool.created == 0
        client.whoami()  # dials a fresh one, everything still works

    assert stub_server.connection_count == 2


def test_stdlib_stream_uses_dedicated_connection(
    generated_package: ModuleType,
    stub_server: StubServer,
) -> None:
    with make_client("http", stub_server.base_url) as client:
        client.whoami()
        assert list(client.inspect_image_archive(IMAGE_UUID, "/etc"))
        client.whoami()

    # one pooled connection for the buffered calls, one for the stream
    assert stub_server.connection_count == 2


def test_connection_pool_caps_idle_connections(
    generated_package: ModuleType,
) -> None:
    http_module = importlib.import_module("contree_client.http")

    class FakeConnection:
        def __init__(self) -> None:
            self.closed = False
            self.sock, self.peer = socket.socketpair()

        def close(self) -> None:
            self.closed = True
            self.sock.close()
            self.peer.close()

    pool = http_module.ConnectionPool(FakeConnection, maxsize=2)
    first, _ = pool.acquire()
    second, _ = pool.acquire()
    assert pool.created == 2

    # the cap is on TOTAL connections: a third borrower blocks until
    # someone returns one instead of dialing past the limit
    borrowed = []
    waiter = threading.Thread(target=lambda: borrowed.append(pool.acquire()))
    waiter.start()
    waiter.join(timeout=0.2)
    assert waiter.is_alive()  # still blocked at capacity
    pool.release(first)
    waiter.join(timeout=2)
    assert borrowed == [(first, True)]

    # discard frees the slot so a fresh dial becomes possible again
    pool.discard(second)
    assert pool.created == 1
    fresh, reused = pool.acquire()
    assert not reused
    pool.release(fresh)
    pool.release(first)
    pool.close()
    assert first.closed
    assert fresh.closed


def test_connection_pool_acquire_respects_deadline(
    generated_package: ModuleType,
) -> None:
    http_module = importlib.import_module("contree_client.http")
    pool = http_module.ConnectionPool(
        lambda: http.client.HTTPConnection("localhost"), maxsize=1
    )
    held, _ = pool.acquire()

    started = time.monotonic()
    with pytest.raises(TimeoutError, match="deadline"):
        pool.acquire(time.monotonic() + 0.03)
    assert time.monotonic() - started < 0.3

    pool.discard(held)


def test_connection_pool_discard_wakes_blocked_borrower(
    generated_package: ModuleType,
) -> None:
    """Discarding a broken connection at capacity must wake a borrower
    blocked in acquire() so it can dial a replacement - the Queue-based
    pool deadlocked here: freeing the slot never unblocked idle.get()."""
    http_module = importlib.import_module("contree_client.http")

    class FakeConnection:
        def __init__(self) -> None:
            self.closed = False
            self.sock, self.peer = socket.socketpair()

        def close(self) -> None:
            self.closed = True
            self.sock.close()
            self.peer.close()

    pool = http_module.ConnectionPool(FakeConnection, maxsize=1)
    held, _ = pool.acquire()

    borrowed = []
    waiter = threading.Thread(target=lambda: borrowed.append(pool.acquire()))
    waiter.start()
    waiter.join(timeout=0.2)
    assert waiter.is_alive()  # blocked: the only slot is borrowed

    pool.discard(held)  # the borrowed connection turned out broken
    waiter.join(timeout=2)
    assert not waiter.is_alive()  # freed slot woke the waiter up
    connection, reused = borrowed[0]
    assert not reused  # a fresh dial, not the discarded connection
    assert connection is not held
    pool.release(connection)
    pool.close()


def test_connection_pool_failed_dial_wakes_blocked_borrower(
    generated_package: ModuleType,
) -> None:
    """A factory failure releases the reserved slot AND wakes a blocked
    borrower - otherwise the slot leaks and the waiter hangs."""
    http_module = importlib.import_module("contree_client.http")

    class FakeConnection:
        def close(self) -> None:
            pass

    dial_started = threading.Event()
    finish_dial = threading.Event()
    dials = []

    def factory() -> FakeConnection:
        dials.append(1)
        if len(dials) == 1:
            dial_started.set()
            finish_dial.wait(timeout=2)
            raise ConnectionRefusedError("dial failed")
        return FakeConnection()

    pool = http_module.ConnectionPool(factory, maxsize=1)

    errors = []

    def failing_borrower() -> None:
        try:
            pool.acquire()
        except ConnectionRefusedError as exc:
            errors.append(exc)

    first = threading.Thread(target=failing_borrower)
    first.start()
    assert dial_started.wait(timeout=2)  # slot reserved, dial underway

    borrowed = []
    waiter = threading.Thread(target=lambda: borrowed.append(pool.acquire()))
    waiter.start()
    waiter.join(timeout=0.2)
    assert waiter.is_alive()  # blocked: the dialing borrower holds the slot

    finish_dial.set()  # the dial fails, releasing the slot
    first.join(timeout=2)
    waiter.join(timeout=2)
    assert not waiter.is_alive()  # the failure woke the waiter up
    assert len(errors) == 1
    assert borrowed[0][1] is False  # ...which then dialed successfully
    assert pool.created == 1
    pool.close()


def test_connection_pool_checks_liveness_before_handing_out(
    generated_package: ModuleType,
) -> None:
    """acquire() must verify an idle connection is still usable: a
    silent socket is handed out, one the peer closed (or wrote to) is
    discarded pre-flight so the request never needs a resend."""
    http_module = importlib.import_module("contree_client.http")

    class FakeConnection:
        def __init__(self) -> None:
            self.sock, self.peer = socket.socketpair()

        def close(self) -> None:
            self.sock.close()
            self.peer.close()

    pool = http_module.ConnectionPool(FakeConnection, maxsize=2)
    healthy, _ = pool.acquire()
    pool.release(healthy)
    again, reused = pool.acquire()
    assert again is healthy  # a silent connection is reusable
    assert reused
    pool.release(healthy)

    healthy.peer.close()  # the server dropped it while idle: EOF
    fresh, reused = pool.acquire()
    assert fresh is not healthy
    assert not reused  # replaced with a fresh dial
    assert pool.created == 1  # the stale one freed its slot

    pool.release(fresh)
    fresh.peer.send(b"unsolicited")  # data while idle: protocol junk
    replacement, reused = pool.acquire()
    assert replacement is not fresh
    assert not reused
    pool.release(replacement)
    pool.close()


def test_stdlib_pool_replaces_dropped_connection_without_resend(
    generated_package: ModuleType,
    stub_server: StubServer,
) -> None:
    """A keepalive connection the server silently dropped is caught by
    the pre-flight liveness check: the next request goes out exactly
    once, on a fresh connection - no send into a dead socket, no
    resend."""
    http_module = importlib.import_module("contree_client.http")

    with make_client("http", stub_server.base_url) as client:
        client.get_operation_status(DROPPING_OPERATION_UUID)
        # the FIN from the server needs an instant to arrive
        pooled = client._pool.idle[-1]
        deadline = time.monotonic() + 2
        while http_module.connection_alive(pooled):
            assert time.monotonic() < deadline, "server never dropped it"
            time.sleep(0.01)

        original = client._send_on
        sends = []

        def counting_send(connection, spec):  # type: ignore[no-untyped-def]
            sends.append(spec.path)
            return original(connection, spec)

        client._send_on = counting_send
        assert client.whoami().permissions

    assert len(sends) == 1  # exactly one send: no doomed attempt
    assert stub_server.connection_count == 2


def test_connection_pool_rejects_invalid_maxsize(
    generated_package: ModuleType,
) -> None:
    http_module = importlib.import_module("contree_client.http")
    with pytest.raises(ValueError, match="maxsize"):
        http_module.ConnectionPool(object, maxsize=0)


def test_stdlib_pool_resends_once_on_stale_keepalive(
    generated_package: ModuleType,
    stub_server: StubServer,
) -> None:
    """A send failing with a stale-keepalive error on a REUSED pooled
    connection discards it and transparently re-sends on a fresh one,
    without leaking a pool slot.

    The pre-flight liveness check cannot see this failure mode: the
    socket looks healthy until the server kills it mid-send, so the
    failure is injected at the send itself.
    """
    with make_client("http", stub_server.base_url) as client:
        client.whoami()  # dial + pool one connection
        original = client._send_on
        failed = []

        def flaky_send(connection, spec):  # type: ignore[no-untyped-def]
            if not failed:
                failed.append(spec)
                raise http.client.BadStatusLine("")
            return original(connection, spec)

        client._send_on = flaky_send
        assert client.whoami().permissions
        assert len(failed) == 1
        assert client._pool.created == 1  # discarded + redialed, no leak
        assert len(client._pool.idle) == 1

    assert stub_server.connection_count == 2


def test_stdlib_pool_never_resends_non_idempotent_requests(
    generated_package: ModuleType,
    stub_server: StubServer,
    exceptions: ModuleType,
) -> None:
    """A stale keepalive during a POST must surface the error instead
    of transparently re-sending: after a lost response the server may
    have executed the request already, and a replayed upload/spawn
    would double the side effect."""
    payload = b"must not be sent twice" * 16
    with make_client("http", stub_server.base_url) as client:
        client.whoami()  # dial + pool one connection
        original = client._send_on
        failed = []

        def flaky_send(connection, spec):  # type: ignore[no-untyped-def]
            if not failed:
                failed.append(spec)
                raise BrokenPipeError("stale keepalive")
            return original(connection, spec)

        client._send_on = flaky_send
        with pytest.raises(exceptions.APIConnectionError) as excinfo:
            client.upload_file(io.BytesIO(payload))
        assert isinstance(excinfo.value.__cause__, BrokenPipeError)
        assert len(failed) == 1  # exactly one attempt, no replay
        assert client._pool.created == 0  # the broken slot was freed

        # the client remains usable afterwards
        assert client.whoami().permissions


def test_stdlib_pool_stale_connection_replays_file_body(
    generated_package: ModuleType,
    stub_server: StubServer,
) -> None:
    """The resend of an IDEMPOTENT request after a stale keepalive
    must replay the file-like body from its initial offset, not from
    wherever the failed send left the cursor."""
    runtime = importlib.import_module("contree_client.runtime")

    payload = b"replayed after a stale keepalive" * 128
    with make_client("http", stub_server.base_url) as client:
        client.whoami()  # dial + pool one connection
        original = client._send_on
        failed = []

        def flaky_send(connection, spec):  # type: ignore[no-untyped-def]
            if not failed:
                failed.append(spec.body.read())  # the send drained it
                raise BrokenPipeError("stale keepalive")
            return original(connection, spec)

        client._send_on = flaky_send
        # the stub route is a POST, but replay safety is the caller's
        # declaration: an idempotent spec opts into the resend
        response = client.request(
            runtime.RequestSpec(
                method="POST",
                path="/files",
                body=io.BytesIO(payload),
                content_type="application/octet-stream",
                idempotent=True,
            )
        )

    # the failed send consumed the whole body...
    assert failed == [payload]
    # ...and the stub hashes exactly the bytes it received: without
    # the rewind the replay would have been empty
    body = json.loads(response.body)
    assert body["sha256"] == hashlib.sha256(payload).hexdigest()
    assert body["size"] == len(payload)
    assert stub_server.connection_count == 2


def test_stdlib_pool_retry_keeps_accounting(
    generated_package: ModuleType,
    stub_server: StubServer,
) -> None:
    """Retries driven by RetryPolicy ride the pool without leaking
    slots: after a flaky exchange the pool is back to one warm idle
    connection."""
    http_module = importlib.import_module("contree_client.http")
    runtime = importlib.import_module("contree_client.runtime")

    client = http_module.ContreeClient(
        TOKEN,
        base_url=stub_server.base_url,
        project=PROJECT,
        retry=runtime.RetryPolicy(delays=(0.0,)),
    )
    with client:
        status = client.get_operation_status(FLAKY_OPERATION_UUID)
        assert status.uuid == FLAKY_OPERATION_UUID
        assert len(client._pool.idle) == 1
        assert client._pool.created == 1

    assert not client._pool.idle
    assert client._pool.created == 0
