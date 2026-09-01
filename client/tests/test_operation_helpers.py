"""wait_operation / follow_operation_events / resolve_image helpers."""

from __future__ import annotations

import asyncio
import importlib
import json
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
    OPERATION_RESPONSE,
    PAYLOAD_FINAL_STATUS_OPERATION_UUID,
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


def test_wait_operation_deadline_bounds_terminal_status_probe(
    invoke: Callable[..., Any], stub_server: StubServer
) -> None:
    started = time.monotonic()
    with pytest.raises(TimeoutError, match=PAYLOAD_STALLED_STATUS_OPERATION_UUID):
        invoke(
            "wait_operation",
            PAYLOAD_STALLED_STATUS_OPERATION_UUID,
            timeout=0.2,
        )
    assert time.monotonic() - started < 1.0


def test_wait_operation_deadline_bounds_final_status_fetch(
    invoke: Callable[..., Any], stub_server: StubServer
) -> None:
    started = time.monotonic()
    with pytest.raises(TimeoutError, match=PAYLOAD_FINAL_STATUS_OPERATION_UUID):
        invoke(
            "wait_operation",
            PAYLOAD_FINAL_STATUS_OPERATION_UUID,
            timeout=0.2,
        )
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


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("cancel_signal", [False, True])
def test_follow_timeout_stops_when_cancelled(
    generated_package: ModuleType,
    asynchronous: bool,
    cancel_signal: bool,
) -> None:
    """Cancellation can surface through status or a stream error."""
    exceptions = importlib.import_module("contree_client.exceptions")
    runtime = importlib.import_module("contree_client.runtime")
    testing = importlib.import_module("contree_client.testing")
    client_class = testing.ContreeAsyncClient if asynchronous else testing.ContreeClient
    client = client_class()
    client.retryable_errors = (exceptions.ContreeTimeoutError,)
    client.mock(
        "iter_operation_events",
        error=exceptions.ContreeTimeoutError("read stalled"),
    )
    statuses = ["CANCELLED"]
    if cancel_signal:
        client.mock(
            "iter_operation_events",
            error=exceptions.SSEStreamError("Operation was cancelled, events dropped"),
        )
        statuses.insert(0, "EXECUTING")
    status_specs = []

    def status_response(spec: Any) -> Any:
        status_specs.append(spec)
        status = statuses.pop(0) if len(statuses) > 1 else statuses[0]
        return runtime.ResponseData(
            status=200,
            headers={"content-type": "application/json"},
            body=json.dumps(
                {
                    **OPERATION_RESPONSE,
                    "uuid": "00000000-0000-0000-0000-00000000dead",
                    "status": status,
                }
            ).encode(),
        )

    if asynchronous:

        async def async_status_response(spec: Any) -> Any:
            return status_response(spec)

        client.request = async_status_response
    else:
        client.request = status_response

    if asynchronous:

        async def collect() -> list[Any]:
            return [
                event
                async for event in client.follow_operation_events(
                    "00000000-0000-0000-0000-00000000dead"
                )
            ]

        events = asyncio.run(collect())
    else:
        events = list(
            client.follow_operation_events("00000000-0000-0000-0000-00000000dead")
        )

    assert events == []
    expected_attempts = 2 if cancel_signal else 1
    assert len(client.calls_for("iter_operation_events")) == expected_attempts
    assert len(status_specs) == expected_attempts


@pytest.mark.parametrize("asynchronous", [False, True])
def test_terminal_probe_bypasses_retry_policy(
    generated_package: ModuleType,
    asynchronous: bool,
) -> None:
    runtime = importlib.import_module("contree_client.runtime")
    testing = importlib.import_module("contree_client.testing")
    client_class = testing.ContreeAsyncClient if asynchronous else testing.ContreeClient
    client = client_class(retry=runtime.RetryPolicy(delays=(0.0,)))
    client.retryable_errors = (ConnectionError,)
    attempts = 0

    def fail(_spec: Any) -> Any:
        nonlocal attempts
        attempts += 1
        raise ConnectionError("status unavailable")

    if asynchronous:

        async def async_fail(spec: Any) -> Any:
            return fail(spec)

        client.request = async_fail
        terminal = asyncio.run(client.operation_terminal(PENDING_OPERATION_UUID))
    else:
        client.request = fail
        terminal = client.operation_terminal(PENDING_OPERATION_UUID)

    assert terminal is False
    assert attempts == 1


@pytest.mark.parametrize("asynchronous", [False, True])
def test_polling_probe_bypasses_retry_policy(
    generated_package: ModuleType,
    asynchronous: bool,
) -> None:
    exceptions = importlib.import_module("contree_client.exceptions")
    runtime = importlib.import_module("contree_client.runtime")
    testing = importlib.import_module("contree_client.testing")
    client_class = testing.ContreeAsyncClient if asynchronous else testing.ContreeClient
    client = client_class(retry=runtime.RetryPolicy(delays=(0.0,)))
    client.mock(
        "iter_operation_events",
        error=exceptions.ContreeAPIError(404, "events unavailable"),
    )
    attempts = 0

    def terminal_status(_spec: Any) -> Any:
        nonlocal attempts
        attempts += 1
        return runtime.ResponseData(
            status=200,
            headers={"content-type": "application/json"},
            body=json.dumps(
                {
                    **OPERATION_RESPONSE,
                    "uuid": PENDING_OPERATION_UUID,
                    "status": "SUCCESS",
                }
            ).encode(),
        )

    def unexpected_call(_spec: Any) -> Any:
        raise AssertionError("polling probe used the retry policy")

    client.call = unexpected_call
    if asynchronous:

        async def async_terminal_status(spec: Any) -> Any:
            return terminal_status(spec)

        client.request = async_terminal_status

        async def collect() -> list[Any]:
            return [
                event
                async for event in client.follow_operation_events(
                    PENDING_OPERATION_UUID
                )
            ]

        events = asyncio.run(collect())
    else:
        client.request = terminal_status
        events = list(client.follow_operation_events(PENDING_OPERATION_UUID))

    assert [event.type for event in events] == ["completion"]
    assert attempts == 1


@pytest.mark.parametrize("asynchronous", [False, True])
def test_wait_operation_retries_final_status_fetch(
    generated_package: ModuleType,
    asynchronous: bool,
) -> None:
    runtime = importlib.import_module("contree_client.runtime")
    testing = importlib.import_module("contree_client.testing")
    client_class = testing.ContreeAsyncClient if asynchronous else testing.ContreeClient
    client = client_class(retry=runtime.RetryPolicy(delays=(0.0,)))
    client.mock("follow_operation_events", [])
    attempts = 0

    def request(_spec: Any) -> Any:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return runtime.ResponseData(
                status=503,
                headers={"content-type": "application/json"},
                body=b'{"error":"retry","status":503}',
            )
        return runtime.ResponseData(
            status=200,
            headers={"content-type": "application/json"},
            body=json.dumps(
                {
                    **OPERATION_RESPONSE,
                    "uuid": PENDING_OPERATION_UUID,
                    "status": "SUCCESS",
                }
            ).encode(),
        )

    if asynchronous:

        async def async_request(spec: Any) -> Any:
            return request(spec)

        client.request = async_request
        operation = asyncio.run(client.wait_operation(PENDING_OPERATION_UUID))
    else:
        client.request = request
        operation = client.wait_operation(PENDING_OPERATION_UUID)

    assert str(operation.status) == "SUCCESS"
    assert attempts == 2


@pytest.mark.parametrize("asynchronous", [False, True])
def test_wait_operation_clamps_final_retry_delay_to_deadline(
    generated_package: ModuleType,
    asynchronous: bool,
) -> None:
    runtime = importlib.import_module("contree_client.runtime")
    testing = importlib.import_module("contree_client.testing")
    client_class = testing.ContreeAsyncClient if asynchronous else testing.ContreeClient
    client = client_class(retry=runtime.RetryPolicy(delays=(1.0,)))
    client.mock("follow_operation_events", [])
    attempts = 0

    def unavailable(_spec: Any) -> Any:
        nonlocal attempts
        attempts += 1
        return runtime.ResponseData(
            status=503,
            headers={"content-type": "application/json"},
            body=b'{"error":"retry","status":503}',
        )

    started = time.monotonic()
    if asynchronous:

        async def async_unavailable(spec: Any) -> Any:
            return unavailable(spec)

        client.request = async_unavailable
        with pytest.raises(TimeoutError, match=PENDING_OPERATION_UUID):
            asyncio.run(client.wait_operation(PENDING_OPERATION_UUID, timeout=0.03))
    else:
        client.request = unavailable
        with pytest.raises(TimeoutError, match=PENDING_OPERATION_UUID):
            client.wait_operation(PENDING_OPERATION_UUID, timeout=0.03)

    assert time.monotonic() - started < 0.3
    assert attempts == 1


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
    # Both the events route and its fallback status request are
    # unavailable. The status transport wait must honor the deadline.
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
