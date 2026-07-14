"""wait_operation / follow_operation_events / resolve_image helpers."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from tests.stub_server import (
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
