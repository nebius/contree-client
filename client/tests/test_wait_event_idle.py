"""Waiter regressions: delayed status updates and silent event streams."""

from __future__ import annotations

import asyncio
import select
import socket
import threading
from collections.abc import Callable, Iterator
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from contree_client import base
from contree_client.httpx import ContreeAsyncClient, ContreeClient
from tests import stub_server as server


@pytest.mark.parametrize("outcome", ["SUCCESS", "FAILED", "CANCELLED"])
def test_completion_before_terminal_status(
    invoke: Callable[..., Any], monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    reads = []

    def route(request: server.Captured, attempts: Any) -> server.Reply:
        if request.path.endswith("/events"):
            return server.Reply(
                status=200,
                content_type="text/event-stream",
                stream_chunks=[server.sse_frame(server.EVENT_COMPLETION)],
            )
        status = "EXECUTING" if not reads else outcome
        reads.append(status)
        return server.json_reply(200, {**server.OPERATION_RESPONSE, "status": status})

    monkeypatch.setattr(server, "route", route)
    result = invoke(
        "wait_operation", server.OPERATION_UUID, follow_events=True, timeout=15
    )
    assert str(result.status) == outcome
    assert reads == ["EXECUTING", outcome]


@pytest.fixture
def silent_events(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    release = threading.Event()
    calls: list[str] = []

    def route(request: server.Captured, attempts: Any) -> server.Reply:
        if request.path.endswith("/events"):
            calls.append("events")
            if calls == ["events"]:
                return server.Reply(
                    status=200,
                    content_type="text/event-stream",
                    stream_chunks=[b": ready\n\n"],
                    stream_release=release,
                )
            assert request.query.get("follow") != ["1"]
            return server.Reply(
                status=200,
                content_type="text/event-stream",
                stream_chunks=[server.sse_frame(server.EVENT_COMPLETION)],
            )
        # The first response must remain open until the waiter returns.
        assert not release.is_set()
        calls.append("status")
        return server.json_reply(200, server.OPERATION_RESPONSE)

    monkeypatch.setattr(server, "route", route)
    try:
        yield calls
    finally:
        release.set()


def test_silent_events_probe_status(
    invoke: Callable[..., Any], silent_events: list[str]
) -> None:
    result = invoke(
        "wait_operation", server.OPERATION_UUID, follow_events=True, timeout=15
    )
    assert str(result.status) == "SUCCESS"
    assert silent_events == ["events", "status", "events", "status"]


@pytest.mark.parametrize("asynchronous", [False, True])
def test_silent_events_release_single_connection(
    stub_server: server.StubServer, silent_events: list[str], asynchronous: bool
) -> None:
    limits = httpx.Limits(max_connections=1, max_keepalive_connections=1)
    if asynchronous:

        async def run() -> Any:
            async with httpx.AsyncClient(limits=limits) as transport:
                client = ContreeAsyncClient(
                    "test", base_url=stub_server.base_url, httpx_client=transport
                )
                return await client.wait_operation(
                    server.OPERATION_UUID, follow_events=True, timeout=15
                )

        result = asyncio.run(run())
    else:
        with httpx.Client(limits=limits) as transport:
            client = ContreeClient(
                "test", base_url=stub_server.base_url, httpx_client=transport
            )
            result = client.wait_operation(
                server.OPERATION_UUID, follow_events=True, timeout=15
            )
    assert str(result.status) == "SUCCESS"
    assert silent_events == ["events", "status", "events", "status"]


@pytest.mark.parametrize("ending", ["pause", "keepalive", "deadline"])
def test_event_progress_through_transport(
    invoke: Callable[..., Any],
    stub_server: server.StubServer,
    monkeypatch: pytest.MonkeyPatch,
    ending: str,
) -> None:
    """Exercise native adapter timers and sockets, including compressed SSE."""
    progress_done = threading.Event()
    release = threading.Event()
    stream_finished = threading.Event()
    idle_interval = base.TIGHT_LOOP_FLOOR
    # The producer runs across several native idle intervals. This timer drives
    # server output; assertions use protocol phases and socket state, not elapsed
    # durations. Mocking stream() or its clock would bypass aiohttp's total timer.
    progress_timer = threading.Timer(3 * idle_interval, progress_done.set)
    stream_socket: socket.socket | None = None
    probes: list[tuple[bool, bool]] = []
    keepalives = 0
    sent = 0
    stream_calls = 0
    send_stream = server.Handler.send_stream

    def peer_closed() -> bool:
        assert stream_socket is not None
        if stream_socket.fileno() < 0:
            return True
        readable, _, _ = select.select([stream_socket], [], [], 0)
        if not readable:
            return False
        try:
            return stream_socket.recv(1, socket.MSG_PEEK) == b""
        except (ConnectionResetError, ConnectionAbortedError):
            # Windows may report a closed connection as WSAECONNABORTED.
            return True

    def capture_socket(handler: server.Handler, reply: server.Reply) -> None:
        nonlocal stream_socket
        if stream_socket is not None:
            send_stream(handler, reply)
            return
        # Keep our observation socket valid when the handler thread exits.
        # EOF on this duplicate proves that the client closed its side.
        stream_socket = handler.connection.dup()
        try:
            send_stream(handler, reply)
        finally:
            stream_finished.set()

    def frames() -> Iterator[bytes]:
        nonlocal keepalives, sent
        progress_timer.start()
        while not progress_done.is_set() and not release.is_set():
            yield server.sse_frame(
                {
                    **server.EVENT_COMPLETION,
                    "id": sent,
                    "type": "stdout",
                    "data": {"value": "x", "encoding": "ascii"},
                }
            )
            sent += 1
        if ending == "deadline":
            # A waiter that renews its overall deadline would reach completion
            # and return success instead of raising TimeoutError.
            yield server.sse_frame({**server.EVENT_COMPLETION, "id": sent})
        elif ending == "keepalive":
            while not release.is_set():
                keepalives += 1
                yield b": keepalive\n\n"
        else:
            release.wait(15)  # Deadlock watchdog; the server does not send EOF.

    def route(request: server.Captured, attempts: Any) -> server.Reply:
        nonlocal stream_calls
        if request.path.endswith("/events"):
            stream_calls += 1
            if stream_calls == 1:
                return server.Reply(
                    status=200, content_type="text/event-stream", stream_chunks=frames()
                )
            return server.Reply(
                status=200,
                content_type="text/event-stream",
                stream_chunks=[
                    server.sse_frame({**server.EVENT_COMPLETION, "id": sent})
                ],
            )
        probes.append((progress_done.is_set(), peer_closed()))
        return server.json_reply(200, server.OPERATION_RESPONSE)

    monkeypatch.setattr(server.Handler, "send_stream", capture_socket)
    monkeypatch.setattr(server, "route", route)
    try:
        if ending == "deadline":
            with pytest.raises(TimeoutError, match=server.OPERATION_UUID):
                invoke(
                    "wait_operation",
                    server.OPERATION_UUID,
                    follow_events=True,
                    timeout=2 * idle_interval,
                )
            assert probes == [], "status was requested while payload events progressed"
            assert peer_closed(), "the expired waiter left its stream open"
        else:
            result = invoke(
                "wait_operation", server.OPERATION_UUID, follow_events=True, timeout=15
            )
            assert str(result.status) == "SUCCESS"
            assert probes == [(True, True), (True, True)], (
                "status probes must follow the progress phase and stream closure",
                probes,
            )
            if ending == "keepalive":
                assert keepalives > 0
        assert sent > 0
    finally:
        release.set()
        progress_timer.cancel()
        try:
            assert stream_finished.wait(15), "server stream did not shut down"
        finally:
            if stream_socket is not None:
                stream_socket.close()


@pytest.mark.parametrize("asynchronous", [False, True])
def test_delayed_status_keeps_original_deadline(
    monkeypatch: pytest.MonkeyPatch, asynchronous: bool
) -> None:
    clock = SimpleNamespace(now=100.0)
    deadlines = []

    def sleep(delay: float) -> None:
        clock.now += delay

    def read(operation_id: str, deadline: float | None) -> Any:
        deadlines.append(deadline)
        if clock.now >= 101.0:
            raise TimeoutError("deadline")
        return SimpleNamespace(status=base.OperationStatus.EXECUTING)

    monkeypatch.setattr(
        base, "time", SimpleNamespace(monotonic=lambda: clock.now, sleep=sleep)
    )
    event = SimpleNamespace(id=1, type="completion")
    if asynchronous:

        async def events(self: Any, *args: Any, **kwargs: Any) -> Any:
            clock.now += 0.75
            yield event

        async def read_async(
            self: Any, operation_id: str, deadline: float | None
        ) -> Any:
            return read(operation_id, deadline)

        async def sleep_async(delay: float) -> None:
            sleep(delay)

        monkeypatch.setattr(
            base,
            "asyncio",
            SimpleNamespace(sleep=sleep_async, TimeoutError=asyncio.TimeoutError),
        )
        monkeypatch.setattr(ContreeAsyncClient, "follow_operation_events", events)
        monkeypatch.setattr(ContreeAsyncClient, "_read_operation_status", read_async)

        async def run() -> None:
            client = ContreeAsyncClient("test")
            try:
                await client.wait_operation(
                    server.OPERATION_UUID, follow_events=True, timeout=1
                )
            finally:
                await client.close()

        with pytest.raises(TimeoutError, match=server.OPERATION_UUID):
            asyncio.run(run())
    else:

        def events(self: Any, *args: Any, **kwargs: Any) -> Any:
            clock.now += 0.75
            yield event

        monkeypatch.setattr(ContreeClient, "follow_operation_events", events)
        monkeypatch.setattr(
            ContreeClient,
            "_read_operation_status",
            lambda self, operation_id, deadline: read(operation_id, deadline),
        )
        with (
            ContreeClient("test") as client,
            pytest.raises(TimeoutError, match=server.OPERATION_UUID),
        ):
            client.wait_operation(server.OPERATION_UUID, follow_events=True, timeout=1)
    assert deadlines == [101.0, 101.0]
    assert clock.now == 101.0
