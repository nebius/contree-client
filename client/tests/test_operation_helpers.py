"""wait_operation / follow_operation_events / resolve_image helpers."""

from __future__ import annotations

from collections.abc import Callable
from types import ModuleType
from typing import Any

import pytest

from tests.stub_server import (
    EVENTS_UNAVAILABLE_OPERATION_UUID,
    EVENTS_UNAVAILABLE_STALLED_OPERATION_UUID,
    EXECUTING_OPERATION_UUID,
    IMAGE_UUID,
    PENDING_OPERATION_UUID,
    RECONNECT_OPERATION_UUID,
    StubServer,
)


def test_wait_operation_is_event_driven(
    invoke: Callable[..., Any], stub_server: StubServer
) -> None:
    operation = invoke("wait_operation", PENDING_OPERATION_UUID)

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
    # one terminality probe on the 425 retry + the final terminal fetch
    assert len(status_requests) == 2


def test_wait_operation_timeout(
    invoke: Callable[..., Any], stub_server: StubServer
) -> None:
    # the events stream keeps ending without a completion frame and
    # the status stays EXECUTING -> the deadline fires
    with pytest.raises(TimeoutError, match=EXECUTING_OPERATION_UUID):
        invoke("wait_operation", EXECUTING_OPERATION_UUID, timeout=0.3)


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


def test_wait_operation_falls_back_to_polling_when_events_missing(
    invoke: Callable[..., Any], stub_server: StubServer
) -> None:
    # /events 404s outright (older backend, proxy that drops the
    # route, ...); the operation itself still finishes
    operation = invoke("wait_operation", EVENTS_UNAVAILABLE_OPERATION_UUID)

    assert str(operation.status) == "SUCCESS"
    events_path = f"/v1/operations/{EVENTS_UNAVAILABLE_OPERATION_UUID}/events"
    events_requests = [c for c in stub_server.captured if c.path == events_path]
    # a 404 means reconnecting will never work: touched exactly once
    assert len(events_requests) == 1
    status_path = f"/v1/operations/{EVENTS_UNAVAILABLE_OPERATION_UUID}"
    status_requests = [c for c in stub_server.captured if c.path == status_path]
    # polled until terminal, plus wait_operation's own final fetch
    assert len(status_requests) >= 3


def test_follow_operation_events_yields_completion_when_events_missing(
    invoke: Callable[..., Any], stub_server: StubServer, models: ModuleType
) -> None:
    # there is no event log to relay, but the caller must still see a
    # terminal completion event, not an iterator that silently ends
    events = invoke(
        "follow_operation_events", EVENTS_UNAVAILABLE_OPERATION_UUID, collect=True
    )

    assert len(events) == 1
    assert events[0].type == "completion"
    assert isinstance(events[0].data, models.EventDataCompletion)
    assert str(events[0].data.status) == "SUCCESS"


def test_wait_operation_polling_fallback_respects_timeout(
    invoke: Callable[..., Any], stub_server: StubServer
) -> None:
    # events unavailable and the operation never finishes: the polling
    # fallback must still honor the deadline instead of spinning forever
    with pytest.raises(TimeoutError, match=EVENTS_UNAVAILABLE_STALLED_OPERATION_UUID):
        invoke(
            "wait_operation",
            EVENTS_UNAVAILABLE_STALLED_OPERATION_UUID,
            timeout=0.3,
        )


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
