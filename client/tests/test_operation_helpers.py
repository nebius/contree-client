"""wait_operation / follow_operation_events / resolve_image helpers."""

from __future__ import annotations

import time
from collections.abc import Callable
from types import ModuleType
from typing import Any

import pytest

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


def test_wait_operation_deadline_bounds_status_probe(
    invoke: Callable[..., Any], stub_server: StubServer
) -> None:
    started = time.monotonic()
    with pytest.raises(TimeoutError, match=PAYLOAD_STALLED_STATUS_OPERATION_UUID):
        invoke("wait_operation", PAYLOAD_STALLED_STATUS_OPERATION_UUID, timeout=0.2)
    assert time.monotonic() - started < 1.0


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


def test_wait_operation_fails_when_events_are_missing(
    invoke: Callable[..., Any], stub_server: StubServer, exceptions: ModuleType
) -> None:
    with pytest.raises(exceptions.APIConnectionError):
        invoke("wait_operation", EVENTS_UNAVAILABLE_OPERATION_UUID)
    events_path = f"/v1/operations/{EVENTS_UNAVAILABLE_OPERATION_UUID}/events"
    events_requests = [c for c in stub_server.captured if c.path == events_path]
    # No status polling or final status fetch may hide the stream failure.
    assert len(events_requests) == 1
    status_path = f"/v1/operations/{EVENTS_UNAVAILABLE_OPERATION_UUID}"
    status_requests = [c for c in stub_server.captured if c.path == status_path]
    assert status_requests == []


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
