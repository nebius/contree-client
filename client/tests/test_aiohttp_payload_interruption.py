"""aiohttp recovery from incomplete chunked operation-event responses."""

from __future__ import annotations

import asyncio
import importlib
from types import ModuleType
from typing import Any

import aiohttp
import pytest

from tests.stub_server import (
    PAYLOAD_INTERRUPTION_OPERATION_UUID,
    PAYLOAD_TIMEOUT_OPERATION_UUID,
    StubServer,
)


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


def test_follow_operation_events_retries_socket_timeout_until_deadline(
    generated_package: ModuleType,
) -> None:
    with pytest.raises(TimeoutError, match=PAYLOAD_TIMEOUT_OPERATION_UUID):
        asyncio.run(follow_after_socket_timeout(delay=0.0, timeout=0.03))
