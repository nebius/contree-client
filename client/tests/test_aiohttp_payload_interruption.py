"""aiohttp recovery from incomplete chunked operation-event responses."""

from __future__ import annotations

import asyncio
import importlib
import time
from types import ModuleType
from typing import Any

import aiohttp
import pytest

from tests.stub_server import (
    PAYLOAD_FINAL_STATUS_OPERATION_UUID,
    PAYLOAD_FORBIDDEN_OPERATION_UUID,
    PAYLOAD_INTERRUPTION_OPERATION_UUID,
    PAYLOAD_STALLED_STATUS_OPERATION_UUID,
    PAYLOAD_TIMEOUT_OPERATION_UUID,
    StubServer,
)


def test_client_payload_error_is_retryable(
    generated_package: ModuleType,
) -> None:
    module = importlib.import_module("contree_client.aiohttp")
    error = aiohttp.ClientPayloadError("incomplete chunked response")

    assert isinstance(error, module.ContreeAsyncClient.retryable_errors)
    assert not isinstance(error, module.ContreeAsyncClient.nonretryable_errors)


async def collect_events(
    base_url: str,
    operation_id: str = PAYLOAD_INTERRUPTION_OPERATION_UUID,
    timeout: float = 1.0,
) -> list[Any]:
    module = importlib.import_module("contree_client.aiohttp")
    async with module.ContreeAsyncClient("test-token", base_url=base_url) as client:
        return [
            event
            async for event in client.follow_operation_events(
                operation_id,
                timeout=timeout,
            )
        ]


def test_follow_operation_events_resumes_after_incomplete_chunked_body(
    generated_package: ModuleType,
    stub_server: StubServer,
) -> None:
    events = asyncio.run(collect_events(stub_server.base_url))

    assert [event.id for event in events] == [0, 1, 2, 3]
    event_requests = [
        request
        for request in stub_server.captured
        if request.path
        == f"/v1/operations/{PAYLOAD_INTERRUPTION_OPERATION_UUID}/events"
    ]
    assert len(event_requests) == 2
    assert event_requests[1].headers["last-event-id"] == "1"


def test_follow_operation_events_preserves_status_from_truncated_error_body(
    generated_package: ModuleType,
    stub_server: StubServer,
    exceptions: ModuleType,
) -> None:
    with pytest.raises(exceptions.ForbiddenError) as caught:
        asyncio.run(
            collect_events(
                stub_server.base_url,
                PAYLOAD_FORBIDDEN_OPERATION_UUID,
                timeout=0.2,
            )
        )

    assert caught.value.status == 403
    event_requests = [
        request
        for request in stub_server.captured
        if request.path == f"/v1/operations/{PAYLOAD_FORBIDDEN_OPERATION_UUID}/events"
    ]
    assert len(event_requests) == 1


async def wait_operation(base_url: str, operation_id: str, timeout: float) -> Any:
    module = importlib.import_module("contree_client.aiohttp")
    async with module.ContreeAsyncClient("test-token", base_url=base_url) as client:
        return await client.wait_operation(operation_id, timeout=timeout)


def test_wait_operation_resumes_after_incomplete_chunked_body(
    generated_package: ModuleType,
    stub_server: StubServer,
) -> None:
    operation = asyncio.run(
        wait_operation(
            stub_server.base_url,
            PAYLOAD_INTERRUPTION_OPERATION_UUID,
            1.0,
        )
    )

    assert str(operation.status) == "SUCCESS"
    event_requests = [
        request
        for request in stub_server.captured
        if request.path
        == f"/v1/operations/{PAYLOAD_INTERRUPTION_OPERATION_UUID}/events"
    ]
    assert len(event_requests) == 2
    assert event_requests[1].headers["last-event-id"] == "1"


def test_wait_operation_keeps_one_deadline_across_payload_interruptions(
    generated_package: ModuleType,
    stub_server: StubServer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = importlib.import_module("contree_client.base")
    monkeypatch.setattr(module, "TIGHT_LOOP_FLOOR", 5.0)
    started = time.monotonic()
    with pytest.raises(TimeoutError, match=PAYLOAD_TIMEOUT_OPERATION_UUID):
        asyncio.run(
            wait_operation(
                stub_server.base_url,
                PAYLOAD_TIMEOUT_OPERATION_UUID,
                0.2,
            )
        )

    assert time.monotonic() - started < 1.0


def test_wait_operation_deadline_bounds_terminal_status_probe(
    generated_package: ModuleType,
    stub_server: StubServer,
) -> None:
    started = time.monotonic()
    with pytest.raises(TimeoutError, match=PAYLOAD_STALLED_STATUS_OPERATION_UUID):
        asyncio.run(
            wait_operation(
                stub_server.base_url,
                PAYLOAD_STALLED_STATUS_OPERATION_UUID,
                0.2,
            )
        )

    assert time.monotonic() - started < 1.0


def test_wait_operation_deadline_bounds_final_status_fetch(
    generated_package: ModuleType,
    stub_server: StubServer,
) -> None:
    started = time.monotonic()
    with pytest.raises(TimeoutError, match=PAYLOAD_FINAL_STATUS_OPERATION_UUID):
        asyncio.run(
            wait_operation(
                stub_server.base_url,
                PAYLOAD_FINAL_STATUS_OPERATION_UUID,
                0.2,
            )
        )

    assert time.monotonic() - started < 1.0


async def follow_after_socket_timeout(delay: float, timeout: float) -> None:
    module = importlib.import_module("contree_client.aiohttp")
    client = module.ContreeAsyncClient("test-token", base_url="http://127.0.0.1")

    async def interrupted_events(*args: Any, **kwargs: Any) -> Any:
        await asyncio.sleep(delay)
        raise aiohttp.SocketTimeoutError("read stalled")
        yield  # pragma: no cover - makes this an async generator

    client.iter_operation_events = interrupted_events
    try:
        async for _event in client.follow_operation_events(
            PAYLOAD_TIMEOUT_OPERATION_UUID,
            timeout=timeout,
        ):
            pass
    finally:
        await client.close()


def test_follow_operation_events_normalizes_socket_timeout_at_deadline(
    generated_package: ModuleType,
) -> None:
    with pytest.raises(TimeoutError, match=PAYLOAD_TIMEOUT_OPERATION_UUID):
        asyncio.run(follow_after_socket_timeout(delay=0.05, timeout=0.01))


def test_follow_operation_events_preserves_socket_timeout_before_deadline(
    generated_package: ModuleType,
) -> None:
    with pytest.raises(aiohttp.SocketTimeoutError, match="read stalled"):
        asyncio.run(follow_after_socket_timeout(delay=0.0, timeout=1.0))


async def wait_after_socket_timeout(delay: float, timeout: float) -> None:
    module = importlib.import_module("contree_client.aiohttp")
    client = module.ContreeAsyncClient("test-token", base_url="http://127.0.0.1")

    async def interrupted_events(*args: Any, **kwargs: Any) -> Any:
        await asyncio.sleep(delay)
        raise aiohttp.SocketTimeoutError("read stalled")
        yield  # pragma: no cover - makes this an async generator

    client.iter_operation_events = interrupted_events
    try:
        await client.wait_operation(PAYLOAD_TIMEOUT_OPERATION_UUID, timeout=timeout)
    finally:
        await client.close()


def test_wait_operation_deadline_preempts_later_socket_timeout(
    generated_package: ModuleType,
) -> None:
    with pytest.raises(TimeoutError, match=PAYLOAD_TIMEOUT_OPERATION_UUID):
        asyncio.run(wait_after_socket_timeout(delay=0.05, timeout=0.01))


def test_wait_operation_preserves_socket_timeout_before_its_deadline(
    generated_package: ModuleType,
) -> None:
    with pytest.raises(aiohttp.SocketTimeoutError, match="read stalled"):
        asyncio.run(wait_after_socket_timeout(delay=0.0, timeout=1.0))
