"""Operation waiters, event followers, and image resolution helpers."""

from __future__ import annotations

import time
from collections import Counter
from collections.abc import Callable
from types import ModuleType
from typing import Any

import pytest

from contree_client import base
from tests import stub_server as server
from tests.stub_server import (
    EVENTS_UNAVAILABLE_OPERATION_UUID,
    EVENTS_UNAVAILABLE_STALLED_OPERATION_UUID,
    EXECUTING_OPERATION_UUID,
    IMAGE_UUID,
    PAYLOAD_FORBIDDEN_OPERATION_UUID,
    PAYLOAD_STALLED_STATUS_OPERATION_UUID,
    PENDING_OPERATION_UUID,
    RECONNECT_OPERATION_UUID,
    StubServer,
)


@pytest.mark.parametrize(
    "kwargs", [{}, {"follow_events": None}, {"follow_events": True}]
)
def test_wait_operation_is_event_driven(
    invoke: Callable[..., Any], stub_server: StubServer, kwargs: dict[str, Any]
) -> None:
    operation = invoke("wait_operation", PENDING_OPERATION_UUID, **kwargs)

    assert str(operation.status) == "SUCCESS"
    events_path = f"/v1/operations/{PENDING_OPERATION_UUID}/events"
    events_requests = [c for c in stub_server.captured if c.path == events_path]
    # the wait follows the SSE stream (425 once, then the log tail
    # with the completion frame) instead of polling the status
    assert len(events_requests) == 2
    status_requests = [
        c
        for c in stub_server.captured
        if c.path == f"/v1/operations/{PENDING_OPERATION_UUID}"
    ]
    # Auto mode detects the kind before following events. Both modes also
    # probe terminality on the 425 retry and fetch the final response.
    auto = kwargs.get("follow_events") is None
    assert len(status_requests) == (3 if auto else 2)
    assert stub_server.captured[0].path == (
        f"/v1/operations/{PENDING_OPERATION_UUID}" if auto else events_path
    )


def test_wait_operation_timeout(
    invoke: Callable[..., Any], stub_server: StubServer
) -> None:
    # the events stream keeps ending without a completion frame and
    # the status stays EXECUTING -> the deadline fires
    with pytest.raises(TimeoutError, match=EXECUTING_OPERATION_UUID):
        invoke("wait_operation", EXECUTING_OPERATION_UUID, timeout=0.3)


def test_wait_operation_deadline_bounds_status_probe(
    invoke: Callable[..., Any], stub_server: StubServer
) -> None:
    started = time.monotonic()
    with pytest.raises(TimeoutError, match=PAYLOAD_STALLED_STATUS_OPERATION_UUID):
        invoke("wait_operation", PAYLOAD_STALLED_STATUS_OPERATION_UUID, timeout=0.2)
    assert time.monotonic() - started < 1.0


@pytest.mark.parametrize("outcome", ["SUCCESS", "FAILED", "CANCELLED"])
@pytest.mark.parametrize(
    ("kind", "follow_events"),
    [("instance", None), ("instance", True), ("image_import", True)],
)
def test_wait_operation_streaming_fetches_final_status(
    invoke: Callable[..., Any],
    stub_server: server.StubServer,
    monkeypatch: pytest.MonkeyPatch,
    outcome: str,
    kind: str,
    follow_events: bool | None,
) -> None:
    detail = None if outcome == "SUCCESS" else "operation failed or was cancelled"
    status_reads = 0

    def route(request: server.Captured, attempts: Counter[str]) -> server.Reply:
        nonlocal status_reads
        if request.path.endswith("/events"):
            completion = {
                **server.EVENT_COMPLETION,
                "data": {**server.EVENT_COMPLETION["data"], "status": outcome},
            }
            return server.Reply(
                status=200,
                content_type="text/event-stream",
                stream_chunks=[server.sse_frame(completion)],
            )
        status_reads += 1
        status = "EXECUTING" if follow_events is None and status_reads == 1 else outcome
        return server.json_reply(
            200,
            {
                **server.OPERATION_RESPONSE,
                "kind": kind,
                "status": status,
                "error": detail,
            },
        )

    monkeypatch.setattr(server, "route", route)
    result = invoke(
        "wait_operation",
        server.OPERATION_UUID,
        timeout=2.0,
        follow_events=follow_events,
    )
    assert str(result.status) == outcome
    assert result.error == detail
    paths = [
        f"/v1/operations/{server.OPERATION_UUID}/events",
        f"/v1/operations/{server.OPERATION_UUID}",
    ]
    if follow_events is None:
        paths.insert(0, f"/v1/operations/{server.OPERATION_UUID}")
    assert [r.path for r in stub_server.captured] == paths


@pytest.mark.parametrize("outcome", ["SUCCESS", "FAILED", "CANCELLED"])
@pytest.mark.parametrize("already_terminal", [False, True])
@pytest.mark.parametrize(
    ("kind", "kwargs"),
    [
        ("image_import", {}),
        ("image_import", {"follow_events": None}),
        ("image_import", {"follow_events": False}),
        ("instance", {"follow_events": False}),
    ],
)
def test_wait_operation_polling_returns_terminal_result(
    invoke: Callable[..., Any],
    stub_server: server.StubServer,
    monkeypatch: pytest.MonkeyPatch,
    outcome: str,
    already_terminal: bool,
    kind: str,
    kwargs: dict[str, Any],
) -> None:
    monkeypatch.setattr(base, "TIGHT_LOOP_FLOOR", 0.01)
    statuses = [] if already_terminal else ["PENDING", "ASSIGNED", "EXECUTING"]
    statuses.append(outcome)
    detail = None if outcome == "SUCCESS" else "registry failure or cancellation"

    def route(request: server.Captured, attempts: Counter[str]) -> server.Reply:
        status = statuses.pop(0)
        payload = {
            **server.OPERATION_RESPONSE,
            "kind": kind,
            "status": status,
            "error": detail,
            "result_image_uuid": server.IMAGE_UUID if status == "SUCCESS" else None,
        }
        return server.json_reply(200, payload)

    monkeypatch.setattr(server, "route", route)
    result = invoke("wait_operation", server.OPERATION_UUID, timeout=5.0, **kwargs)
    assert str(result.status) == outcome
    assert result.error == detail
    assert result.result_image_uuid == (
        server.IMAGE_UUID if outcome == "SUCCESS" else None
    )
    assert len(stub_server.captured) == (1 if already_terminal else 4)
    assert all(
        r.method == "GET" and r.path == f"/v1/operations/{server.OPERATION_UUID}"
        for r in stub_server.captured
    )


@pytest.mark.parametrize("stalled_read", [None, 1, 2])
@pytest.mark.parametrize("follow_events", [None, False])
def test_wait_operation_polling_respects_deadline(
    invoke: Callable[..., Any],
    stub_server: server.StubServer,
    monkeypatch: pytest.MonkeyPatch,
    stalled_read: int | None,
    follow_events: bool | None,
) -> None:
    reads = 0

    def route(request: server.Captured, attempts: Counter[str]) -> server.Reply:
        nonlocal reads
        reads += 1
        if stalled_read is not None and reads >= stalled_read:
            time.sleep(2.0)
        return server.json_reply(
            200,
            {
                **server.OPERATION_RESPONSE,
                "kind": "image_import",
                "status": "EXECUTING",
            },
        )

    monkeypatch.setattr(server, "route", route)
    started = time.monotonic()
    with pytest.raises(TimeoutError, match=server.OPERATION_UUID):
        invoke(
            "wait_operation",
            server.OPERATION_UUID,
            timeout=0.7,
            follow_events=follow_events,
        )
    assert time.monotonic() - started < 1.5
    assert all(r.method == "GET" for r in stub_server.captured)
    assert not any(r.path.endswith("/events") for r in stub_server.captured)


def test_follow_operation_events_reconnects(
    invoke: Callable[..., Any], stub_server: StubServer
) -> None:
    events = invoke("follow_operation_events", RECONNECT_OPERATION_UUID, collect=True)

    assert [event.id for event in events] == [0, 1, 2, 3]
    assert events[-1].type == "completion"
    stream_requests = [
        c
        for c in stub_server.captured
        if c.path == f"/v1/operations/{RECONNECT_OPERATION_UUID}/events"
    ]
    assert len(stream_requests) == 2
    # the reconnect resumes from the last received event id
    assert stream_requests[1].headers["last-event-id"] == "1"


@pytest.mark.parametrize("follow_events", [None, True])
def test_wait_operation_fails_when_events_are_missing(
    invoke: Callable[..., Any],
    stub_server: StubServer,
    exceptions: ModuleType,
    follow_events: bool | None,
) -> None:
    with pytest.raises(exceptions.APIConnectionError):
        invoke(
            "wait_operation",
            EVENTS_UNAVAILABLE_OPERATION_UUID,
            follow_events=follow_events,
        )
    events_path = f"/v1/operations/{EVENTS_UNAVAILABLE_OPERATION_UUID}/events"
    events_requests = [c for c in stub_server.captured if c.path == events_path]
    # Kind detection must not hide a stream failure by falling back to polling.
    assert len(events_requests) == 1
    status_path = f"/v1/operations/{EVENTS_UNAVAILABLE_OPERATION_UUID}"
    status_requests = [c for c in stub_server.captured if c.path == status_path]
    assert len(status_requests) == (1 if follow_events is None else 0)


def test_follow_operation_events_reports_unavailable_log(
    invoke: Callable[..., Any], stub_server: StubServer, exceptions: ModuleType
) -> None:
    with pytest.raises(exceptions.APIConnectionError):
        invoke(
            "follow_operation_events", EVENTS_UNAVAILABLE_OPERATION_UUID, collect=True
        )


def test_wait_operation_missing_events_fails_even_with_deadline(
    invoke: Callable[..., Any], stub_server: StubServer, exceptions: ModuleType
) -> None:
    with pytest.raises(exceptions.APIConnectionError):
        invoke(
            "wait_operation",
            EVENTS_UNAVAILABLE_STALLED_OPERATION_UUID,
            timeout=0.3,
            follow_events=True,
        )


def test_permanent_stream_failure_does_not_probe_status(
    invoke: Callable[..., Any],
    stub_server: StubServer,
    exceptions: ModuleType,
) -> None:
    with pytest.raises(exceptions.APIConnectionError) as caught:
        invoke(
            "follow_operation_events",
            PAYLOAD_FORBIDDEN_OPERATION_UUID,
            collect=True,
        )
    assert isinstance(caught.value.__cause__, exceptions.PermissionDeniedError)
    assert all(request.path.endswith("/events") for request in stub_server.captured)


def test_resolve_image_tag_prefix(
    invoke: Callable[..., Any], stub_server: StubServer
) -> None:
    assert invoke("resolve_image", "tag:busybox:latest") == IMAGE_UUID


def test_resolve_image_bare_tag(
    invoke: Callable[..., Any], stub_server: StubServer
) -> None:
    assert invoke("resolve_image", "busybox:latest") == IMAGE_UUID


def test_resolve_image_uuid_passthrough(
    invoke: Callable[..., Any], stub_server: StubServer
) -> None:
    assert invoke("resolve_image", IMAGE_UUID) == IMAGE_UUID
    assert not stub_server.captured
