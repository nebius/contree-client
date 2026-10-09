"""A terminal operation may still have events this subscriber has not read."""

from __future__ import annotations

import asyncio
import importlib
import json
from collections.abc import Callable
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from tests.stub_server import (
    EVENT_COMPLETION,
    EVENT_EXIT,
    EVENT_SPAWN,
    OPERATION_RESPONSE,
    OPERATION_UUID,
    StubServer,
    sse_frame,
)


class RecoveryScenario:
    """Script transport reads while exercising the real SSE parser and helpers."""

    def __init__(self) -> None:
        self.now = 100.0
        self.sleeps: list[float] = []
        self.attempts: list[Any] = []
        self.requests: list[Any] = []
        self.scripts: list[list[bytes | Exception | float]] = []
        self.received: list[Any] = []
        self.probe_duration = 0.0

    def sleep(self, delay: float) -> None:
        self.sleeps.append(delay)
        self.now += delay

    async def async_sleep(self, delay: float) -> None:
        self.sleep(delay)

    def stream(self, spec: Any):
        self.attempts.append(spec)
        if not self.scripts:
            raise AssertionError("unexpected extra event-log request")
        for item in self.scripts.pop(0):
            if isinstance(item, Exception):
                raise item
            if isinstance(item, float):
                self.now += item
            else:
                yield item

    def collect(self, method: str = "follow_operation_events", **options: Any) -> Any:
        options.setdefault("timeout", 10.0)
        source = getattr(self.client, method)(OPERATION_UUID, **options)
        if self.async_mode:

            async def run():
                if method == "wait_operation":
                    return await source
                async for event in source:
                    self.received.append(event)
                return self.received

            return asyncio.run(run())
        if method == "wait_operation":
            return source
        self.received.extend(source)
        return self.received


@pytest.fixture(params=(False, True), ids=("sync", "async"))
def recovery(
    request: pytest.FixtureRequest,
    generated_package: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> RecoveryScenario:
    base = importlib.import_module("contree_client.base")
    runtime = importlib.import_module("contree_client.runtime")
    scenario = RecoveryScenario()
    scenario.async_mode = request.param
    monkeypatch.setattr(
        base,
        "time",
        SimpleNamespace(monotonic=lambda: scenario.now, sleep=scenario.sleep),
    )
    monkeypatch.setattr(
        base,
        "asyncio",
        SimpleNamespace(**{**vars(asyncio), "sleep": scenario.async_sleep}),
    )

    def response(spec):
        scenario.requests.append(spec)
        scenario.now += scenario.probe_duration
        return runtime.ResponseData(
            status=200, headers={}, body=json.dumps(OPERATION_RESPONSE).encode()
        )

    class SyncClient(base.ContreeSyncClient):
        def request(self, spec):
            return response(spec)

        def stream(self, spec, auto_decompress=True):
            yield from scenario.stream(spec)

        def close(self):
            pass

    class AsyncClient(base.ContreeAsyncClient):
        async def request(self, spec):
            return response(spec)

        async def stream(self, spec, auto_decompress=True):
            for chunk in scenario.stream(spec):
                yield chunk

        async def close(self):
            pass

    client_class = AsyncClient if scenario.async_mode else SyncClient
    scenario.client = client_class(
        "token", retry=runtime.RetryPolicy(delays=(0.0,), max_attempts=3)
    )
    return scenario


@pytest.mark.parametrize("ending", ("broken", "eof", "sse_error"))
def test_terminal_probe_drains_unread_tail(recovery: RecoveryScenario, ending: str):
    initial = [sse_frame(EVENT_SPAWN)]
    if ending == "broken":
        initial.append(EOFError("truncated gzip SSE"))
    elif ending == "sse_error":
        initial.append(b"event: sse_error\ndata: upstream closed\n\n")
    recovery.scripts = [initial, [sse_frame(EVENT_EXIT), sse_frame(EVENT_COMPLETION)]]

    events = recovery.collect()

    assert [event.id for event in events] == [1, 2, 3]
    assert [event.type for event in events] == ["spawn", "exit", "completion"]
    assert len(recovery.attempts) == 2
    assert len(recovery.requests) == 1
    assert recovery.attempts[0].query.get("follow") == "1"
    assert recovery.attempts[1].query.get("follow", "0") == "0"
    assert recovery.attempts[1].headers["Last-Event-Id"] == "1"


def test_interrupted_retained_read_resumes_at_updated_cursor(
    recovery: RecoveryScenario,
):
    recovery.scripts = [
        [sse_frame(EVENT_SPAWN), EOFError("live stream broke")],
        [sse_frame(EVENT_EXIT), EOFError("retained stream broke")],
        [sse_frame(EVENT_COMPLETION)],
    ]

    assert [event.id for event in recovery.collect()] == [1, 2, 3]
    assert [spec.headers.get("Last-Event-Id") for spec in recovery.attempts] == [
        None,
        "1",
        "2",
    ]
    assert all(spec.query.get("follow", "0") == "0" for spec in recovery.attempts[1:])


@pytest.mark.parametrize(
    "prefix",
    (b"", b"id: 1\nevent: spawn\ndata: {"),
    ids=("no-frame", "partial-frame"),
)
def test_break_before_first_complete_frame_replays_from_start(
    recovery: RecoveryScenario, prefix: bytes
):
    recovery.scripts = [
        [prefix, EOFError("truncated first frame")],
        [sse_frame(EVENT_SPAWN), sse_frame(EVENT_EXIT), sse_frame(EVENT_COMPLETION)],
    ]

    assert [event.id for event in recovery.collect()] == [1, 2, 3]
    assert len(recovery.attempts) == 2
    assert "Last-Event-Id" not in recovery.attempts[1].headers


def test_completion_ends_iteration_without_status_probe(recovery: RecoveryScenario):
    recovery.scripts = [[sse_frame(EVENT_SPAWN), sse_frame(EVENT_COMPLETION)]]

    assert [event.id for event in recovery.collect()] == [1, 3]
    assert len(recovery.attempts) == 1
    assert recovery.requests == []


def test_wait_operation_fetches_status_after_recovering_completion(
    recovery: RecoveryScenario,
):
    recovery.scripts = [
        [sse_frame(EVENT_SPAWN), EOFError("live stream broke")],
        [sse_frame(EVENT_EXIT), sse_frame(EVENT_COMPLETION)],
    ]

    operation = recovery.collect("wait_operation")

    assert str(operation.status) == "SUCCESS"
    assert len(recovery.attempts) == 2
    assert recovery.scripts == []
    assert len(recovery.requests) == 2  # terminal probe, then final status fetch


def test_retained_artifact_not_ready_honors_retry_after(recovery: RecoveryScenario):
    exceptions = importlib.import_module("contree_client.exceptions")
    recovery.scripts = [
        [sse_frame(EVENT_SPAWN), EOFError("live stream broke")],
        [exceptions.APIStatusError(410, "artifact not durable", retry_after=2)],
        [sse_frame(EVENT_EXIT), sse_frame(EVENT_COMPLETION)],
    ]

    assert [event.id for event in recovery.collect()] == [1, 2, 3]
    assert sum(recovery.sleeps) >= 2.0
    assert recovery.attempts[2].headers["Last-Event-Id"] == "1"
    assert all(spec.deadline == 110.0 for spec in recovery.attempts)


@pytest.mark.parametrize("failure", ("unavailable", "incomplete", "interrupted"))
@pytest.mark.parametrize("method", ("follow_operation_events", "wait_operation"))
def test_retained_log_failure_is_not_silent_success(
    recovery: RecoveryScenario, failure: str, method: str
):
    exceptions = importlib.import_module("contree_client.exceptions")
    retained = {
        "unavailable": [exceptions.NotFoundError(404, "event log missing")],
        "incomplete": [sse_frame(EVENT_EXIT)],
        "interrupted": [EOFError("retained stream broke")],
    }[failure]
    recovery.scripts = [[sse_frame(EVENT_SPAWN), EOFError("live stream broke")]]
    recovery.scripts.extend([list(retained) for _ in range(3)])

    with pytest.raises(exceptions.APIConnectionError):
        recovery.collect(method)

    assert len(recovery.requests) == 1  # no final fetch hiding failed recovery
    assert 2 <= len(recovery.attempts) <= 4
    if failure == "interrupted":
        assert len(recovery.attempts) == 4  # three retained-read attempts


@pytest.mark.parametrize(
    "options",
    ({"spid": 1}, {"since": 0}, {"last_event_id": 0}),
    ids=("spid", "since", "last-event-id"),
)
def test_filtered_or_resumed_retained_log_can_end_without_completion(
    recovery: RecoveryScenario, options: dict[str, int]
):
    recovery.scripts = [
        [sse_frame(EVENT_SPAWN), EOFError("live stream broke")],
        [sse_frame(EVENT_EXIT)],
    ]

    assert [event.id for event in recovery.collect(**options)] == [1, 2]
    assert len(recovery.attempts) == 2
    for name in ("spid", "since"):
        if name in options:
            assert all(
                spec.query[name] == str(options[name]) for spec in recovery.attempts
            )
    assert recovery.attempts[0].headers.get("Last-Event-Id") == (
        "0" if "last_event_id" in options else None
    )
    assert recovery.attempts[1].headers["Last-Event-Id"] == "1"


def test_retained_read_uses_remaining_original_deadline(recovery: RecoveryScenario):
    recovery.probe_duration = 2.0
    recovery.scripts = [
        [sse_frame(EVENT_SPAWN), 3.0, EOFError("live stream broke")],
        [sse_frame(EVENT_EXIT), 5.0, sse_frame(EVENT_COMPLETION)],
    ]

    with pytest.raises(TimeoutError):
        recovery.collect()

    assert [event.id for event in recovery.received] == [1, 2]
    assert [spec.deadline for spec in recovery.attempts] == [110.0, 110.0]
    assert [spec.read_timeout for spec in recovery.attempts] == [10.0, 5.0]


def test_retry_after_cannot_extend_recovery_deadline(recovery: RecoveryScenario):
    exceptions = importlib.import_module("contree_client.exceptions")
    recovery.scripts = [
        [sse_frame(EVENT_SPAWN), EOFError("live stream broke")],
        [exceptions.APIStatusError(410, "artifact not durable", retry_after=20)],
        [sse_frame(EVENT_EXIT), sse_frame(EVENT_COMPLETION)],
    ]

    with pytest.raises(TimeoutError):
        recovery.collect(timeout=1.0)

    assert len(recovery.attempts) == 2
    assert recovery.now == 101.0


def test_terminal_recovery_retries_http_410_across_backends(
    invoke: Callable[..., Any],
    stub_server: StubServer,
    monkeypatch: pytest.MonkeyPatch,
):
    stub = importlib.import_module("tests.stub_server")
    original_route = stub.route
    event_requests = []

    def route(request, attempts):
        if request.path != f"/v1/operations/{OPERATION_UUID}/events":
            return original_route(request, attempts)
        event_requests.append(request)
        if len(event_requests) == 2:
            return stub.json_reply(
                410, {"error": "artifact not durable"}, **{"Retry-After": "0"}
            )
        events = (
            [EVENT_SPAWN]
            if len(event_requests) == 1
            else [EVENT_EXIT, EVENT_COMPLETION]
        )
        return stub.Reply(
            status=200,
            content_type="text/event-stream",
            stream_chunks=[sse_frame(event) for event in events],
        )

    monkeypatch.setattr(stub, "route", route)

    events = invoke(
        "follow_operation_events", OPERATION_UUID, timeout=5.0, collect=True
    )

    assert [event.id for event in events] == [1, 2, 3]
    assert len(event_requests) == 3
    assert all(
        request.headers["last-event-id"] == "1" for request in event_requests[1:]
    )
    assert all("follow" not in request.query for request in event_requests[1:])
