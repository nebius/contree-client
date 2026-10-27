"""Waiter regressions: delayed status updates and silent event streams."""

from __future__ import annotations

import asyncio
import json
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


@pytest.mark.parametrize("asynchronous", [False, True])
def test_event_progress_resets_idle_deadline(
    monkeypatch: pytest.MonkeyPatch, asynchronous: bool
) -> None:
    # Advance a fake clock through several idle intervals. Payload events reset
    # the idle deadline; keepalives do not. Status reads require a closed stream.
    clock = SimpleNamespace(now=100.0)
    monkeypatch.setattr(
        base, "time", SimpleNamespace(monotonic=lambda: clock.now, sleep=lambda _: None)
    )
    calls = []
    deadlines = []
    closed = threading.Event()

    def chunks(spec: Any) -> Iterator[bytes]:
        calls.append("events")
        try:
            if calls == ["events"]:
                for event_id in range(4):
                    assert spec.deadline == pytest.approx(clock.now + 0.5)
                    clock.now += 0.4
                    yield server.sse_frame(
                        {
                            **server.EVENT_COMPLETION,
                            "id": event_id,
                            "type": "stdout",
                            "data": {"value": "x", "encoding": "ascii"},
                        }
                    )
                    assert calls == ["events"]
                clock.now += 0.5
                # A real transport rejects the next read at this deadline.
                assert spec.deadline == pytest.approx(clock.now)
                raise TimeoutError("idle")
            yield server.sse_frame(server.EVENT_COMPLETION)
        finally:
            closed.set()

    def response(spec: Any) -> Any:
        assert closed.is_set()
        calls.append("status")
        deadlines.append(spec.deadline)
        return base.ResponseData(
            200, {}, json.dumps(server.OPERATION_RESPONSE).encode()
        )

    if asynchronous:

        async def async_chunks(self: Any, spec: Any, **kwargs: Any) -> Any:
            for chunk in chunks(spec):
                yield chunk

        async def async_response(self: Any, spec: Any) -> Any:
            return response(spec)

        monkeypatch.setattr(ContreeAsyncClient, "stream", async_chunks)
        monkeypatch.setattr(ContreeAsyncClient, "request", async_response)

        async def run() -> Any:
            client = ContreeAsyncClient("test")
            try:
                return await client.wait_operation(
                    server.OPERATION_UUID, timeout=10, follow_events=True
                )
            finally:
                await client.close()

        result = asyncio.run(run())
    else:
        monkeypatch.setattr(ContreeClient, "stream", lambda self, spec: chunks(spec))
        monkeypatch.setattr(ContreeClient, "request", lambda self, spec: response(spec))
        with ContreeClient("test") as client:
            result = client.wait_operation(
                server.OPERATION_UUID, timeout=10, follow_events=True
            )
    assert str(result.status) == "SUCCESS"
    assert calls == ["events", "status", "events", "status"]
    assert deadlines == [110.0, 110.0]


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
