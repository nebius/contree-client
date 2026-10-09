"""Async stream lifecycle: early close must release the transport.

Regression tests for improvements.md P1-08: the public async
generators wrap ``self.stream(...)`` / ``iter_operation_events(...)``
in ``contextlib.aclosing``, so ``aclose()`` propagates immediately
instead of waiting for non-deterministic async finalization.
"""

from __future__ import annotations

import asyncio
import importlib
import io
import json
from collections.abc import AsyncIterator, Awaitable
from types import ModuleType, SimpleNamespace
from typing import Any

import httpx
import pytest
import urllib3

from contree_client.exceptions import APIConnectionError
from tests import stub_server as server
from tests.stub_server import OPERATION_RESPONSE, OPERATION_UUID


def make_tracking_client(generated_package: ModuleType) -> object:
    testing = importlib.import_module("contree_client.testing")

    class TrackingClient(testing.ContreeAsyncClient):
        def __init__(self) -> None:
            super().__init__()
            self.stream_closed = False

        def stream(
            self, spec: object, auto_decompress: bool = True
        ) -> AsyncIterator[bytes]:
            async def generator() -> AsyncIterator[bytes]:
                try:
                    while True:
                        yield b"chunk"
                finally:
                    self.stream_closed = True

            return generator()

    return TrackingClient()


UUID = "12345678-9abc-baba-deda-0123456789ab"


class FakeClock:
    def __init__(self, now: float) -> None:
        self.now = now

    def monotonic(self) -> float:
        return self.now


@pytest.mark.parametrize(
    "backend", ("http", "urllib3", "requests", "httpx", "httpx_async", "aiohttp")
)
def test_follow_retries_connection_error_before_deadline(
    generated_package: ModuleType,
    backend: str,
) -> None:
    module_name = "httpx" if backend == "httpx_async" else backend
    module = importlib.import_module(f"contree_client.{module_name}")
    models = importlib.import_module("contree_client.models")
    async_backend = backend in ("httpx_async", "aiohttp")
    client_class = module.ContreeAsyncClient if async_backend else module.ContreeClient
    client = client_class("token", timeout=0.01)
    completion = models.OperationEvent.from_dict(
        {
            "id": 7,
            "ts": "2026-06-08T20:00:00Z",
            "spid": 0,
            "type": "completion",
            "data": {"status": "SUCCESS"},
        }
    )
    attempts = 0
    probes = 0
    if async_backend:

        async def events(*args: Any, **kwargs: Any) -> AsyncIterator[Any]:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise APIConnectionError("read timed out", timed_out=True)
            yield completion

        async def status(*args: Any, **kwargs: Any) -> bool:
            nonlocal probes
            probes += 1
            return True

        client.iter_operation_events = events
        client.operation_terminal = status

        async def scenario() -> None:
            try:
                result = [
                    event
                    async for event in client.follow_operation_events(
                        UUID, timeout=10.0
                    )
                ]
                assert result == [completion]
            finally:
                await client.close()

        asyncio.run(scenario())
    else:

        def events(*args: Any, **kwargs: Any):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise APIConnectionError("read timed out", timed_out=True)
            yield completion

        def status(*args: Any, **kwargs: Any) -> bool:
            nonlocal probes
            probes += 1
            return True

        client.iter_operation_events = events
        client.operation_terminal = status
        try:
            assert list(client.follow_operation_events(UUID, timeout=10.0)) == [
                completion
            ]
        finally:
            client.close()
    assert attempts == 2
    assert probes == 1


@pytest.mark.parametrize(
    ("method_name", "options"),
    (
        ("iter_operation_events", {"deadline": 110.0}),
        ("follow_operation_events", {"timeout": 10.0}),
    ),
)
def test_event_deadline_is_checked_when_consumer_resumes(
    generated_package: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    method_name: str,
    options: dict[str, float],
) -> None:
    base = importlib.import_module("contree_client.base")
    runtime = importlib.import_module("contree_client.runtime")
    clock = FakeClock(100.0)
    monkeypatch.setattr(base, "time", SimpleNamespace(monotonic=clock.monotonic))

    class DeadlineClient(base.ContreeSyncClient):
        stream_resumed = False

        def request(self, spec: runtime.RequestSpec) -> runtime.ResponseData:
            raise NotImplementedError

        def stream(self, spec: object, auto_decompress: bool = True):
            yield (
                b"id: 1\nevent: stdout\n"
                b'data: {"id":1,"ts":"2026-06-08T20:00:00Z",'
                b'"spid":1,"type":"stdout","data":{"value":"x"}}\n\n'
            )
            self.stream_resumed = True
            yield b""

        def close(self) -> None:
            pass

    client = DeadlineClient("token")
    events = getattr(client, method_name)(UUID, **options)
    assert next(events).id == 1

    clock.now = 110.0
    with pytest.raises(TimeoutError):
        next(events)
    assert client.stream_resumed is False


def test_httpx_async_stream_recomputes_deadline_before_each_read(
    generated_package: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = importlib.import_module("contree_client.httpx")
    runtime = importlib.import_module("contree_client.runtime")
    clock = FakeClock(100.0)
    monkeypatch.setattr(runtime, "monotonic", clock.monotonic)
    timeouts: list[float | None] = []

    async def capture_wait(
        awaitable: Awaitable[bytes],
        timeout: float | None,
    ) -> bytes:
        timeouts.append(timeout)
        return await awaitable

    monkeypatch.setattr(
        module,
        "asyncio",
        SimpleNamespace(wait_for=capture_wait, TimeoutError=asyncio.TimeoutError),
    )

    class FakeStream(httpx.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            yield b"first"
            yield b"second"

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=FakeStream())

    async def scenario() -> None:
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as httpx_client:
            client = module.ContreeAsyncClient(
                "token",
                base_url="http://example.test",
                httpx_client=httpx_client,
            )
            source = client.stream(
                runtime.RequestSpec(method="GET", path="/x", deadline=110.0)
            )
            assert await anext(source) == b"first"
            clock.now = 104.0
            assert await anext(source) == b"second"
            await source.aclose()

    asyncio.run(scenario())
    assert timeouts == [10.0, 6.0]


def test_archive_early_aclose_closes_transport_stream(
    generated_package: ModuleType,
) -> None:
    client = make_tracking_client(generated_package)

    async def scenario() -> None:
        source = client.inspect_image_archive(UUID, "/etc")
        assert await source.__anext__() == b"chunk"
        await source.aclose()
        # synchronously, not via garbage collection
        assert client.stream_closed

    asyncio.run(scenario())


def test_download_stream_early_aclose_closes_transport_stream(
    generated_package: ModuleType,
) -> None:
    client = make_tracking_client(generated_package)

    async def scenario() -> None:
        source = client.inspect_image_download_stream(UUID, "/etc/hosts")
        assert await source.__anext__() == b"chunk"
        await source.aclose()
        assert client.stream_closed

    asyncio.run(scenario())


def test_follow_early_aclose_closes_inner_iterator(
    generated_package: ModuleType,
) -> None:
    testing = importlib.import_module("contree_client.testing")
    models = importlib.import_module("contree_client.models")

    event = models.OperationEvent.from_dict(
        {
            "id": 1,
            "ts": "2026-06-08T20:00:00Z",
            "spid": 1,
            "type": "stdout",
            "data": {"value": "hi\n", "encoding": "ascii"},
        }
    )

    class TrackingClient(testing.ContreeAsyncClient):
        def __init__(self) -> None:
            super().__init__()
            self.inner_closed = False

        def iter_operation_events(self, *args: object, **kwargs: object):
            async def generator():
                try:
                    while True:
                        yield event
                finally:
                    self.inner_closed = True

            return generator()

    client = TrackingClient()

    async def scenario() -> None:
        source = client.follow_operation_events(UUID)
        assert (await source.__anext__()) is event
        await source.aclose()
        assert client.inner_closed

    asyncio.run(scenario())


def test_follow_reconnects_after_truncated_stream(
    generated_package: ModuleType,
) -> None:
    """A broken stream triggers the terminal operation probe."""
    base = importlib.import_module("contree_client.base")
    runtime = importlib.import_module("contree_client.runtime")

    class TruncatedStreamClient(base.ContreeSyncClient):
        def __init__(self) -> None:
            super().__init__("token")
            self.stream_attempts = 0

        def request(self, spec: runtime.RequestSpec) -> runtime.ResponseData:
            # the terminal probe: the operation is already done
            return runtime.ResponseData(
                status=200,
                headers={},
                body=json.dumps(OPERATION_RESPONSE).encode(),
            )

        def stream(self, spec, auto_decompress=True):  # type: ignore[no-untyped-def]
            self.stream_attempts += 1
            raise APIConnectionError("truncated gzip SSE") from EOFError("truncated")
            yield b""  # pragma: no cover - makes this a generator

        def close(self) -> None:
            pass

    client = TruncatedStreamClient()
    exceptions = importlib.import_module("contree_client.exceptions")
    with pytest.raises(exceptions.APIConnectionError) as caught:
        list(client.follow_operation_events(OPERATION_UUID))
    assert isinstance(caught.value.__cause__, APIConnectionError)
    assert isinstance(caught.value.__cause__.__cause__, EOFError)
    # One live read, then three bounded attempts to drain the terminal log.
    assert client.stream_attempts == 4


def test_sse_id_only_frames_advance_the_resume_cursor(
    generated_package: ModuleType,
) -> None:
    """P2-16: an id-only frame carries no payload but must advance the
    Last-Event-Id cursor used for reconnects."""
    base = importlib.import_module("contree_client.base")
    runtime = importlib.import_module("contree_client.runtime")

    class IdOnlyClient(base.ContreeSyncClient):
        def __init__(self) -> None:
            super().__init__("token")

        def request(self, spec: runtime.RequestSpec) -> runtime.ResponseData:
            raise NotImplementedError

        def stream(self, spec, auto_decompress=True):  # type: ignore[no-untyped-def]
            yield b"id: 5\n\n"
            yield b"event: sse_error\ndata: boom\n\n"

        def close(self) -> None:
            pass

    client = IdOnlyClient()
    with pytest.raises(APIConnectionError) as caught:
        list(client.iter_operation_events("00000000-0000-0000-0000-000000000000"))
    assert caught.value.__dict__["last_event_id"] == 5


@pytest.mark.parametrize("failure_phase", ["follow", "drain"])
def test_urllib3_proxy_failure_recovers(
    generated_package: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    failure_phase: str,
) -> None:
    module = importlib.import_module("contree_client.urllib3")
    runtime = importlib.import_module("contree_client.runtime")
    proxy = urllib3.ProxyManager("http://127.0.0.1:8080")
    requests_seen: list[tuple[bool, str | None]] = []
    failed = False
    probes = 0

    def request(method: str, url: str, **kwargs: Any) -> urllib3.HTTPResponse:
        nonlocal failed, probes
        if "/events" not in url:
            probes += 1
            return urllib3.HTTPResponse(
                status=200, body=json.dumps(OPERATION_RESPONSE).encode()
            )
        following = "follow=1" in url
        cursor = kwargs["headers"].get("Last-Event-Id")
        requests_seen.append((following, cursor))
        assert kwargs["retries"] is False
        if not failed and following == (failure_phase == "follow"):
            failed = True
            raise urllib3.exceptions.ProxyError(
                "proxy connection failed", ConnectionResetError("proxy reset")
            )
        events = (
            [server.EVENT_INIT]
            if following
            else [
                server.EVENT_INIT,
                server.EVENT_SPAWN,
                server.EVENT_EXIT,
                server.EVENT_COMPLETION,
            ]
        )
        body = b"".join(
            server.sse_frame(event)
            for event in events
            if cursor is None or event["id"] > int(cursor)
        )
        return urllib3.HTTPResponse(
            status=200, body=io.BytesIO(body), preload_content=False
        )

    monkeypatch.setattr(proxy, "request", request)
    try:
        with module.ContreeClient(
            "token",
            urllib3_pool_manager=proxy,
            retry=runtime.RetryPolicy(max_attempts=2, delays=(0.0,)),
        ) as client:
            events = list(client.follow_operation_events(OPERATION_UUID, timeout=1.0))
        assert [event.id for event in events] == [0, 1, 2, 3]
        assert probes == 1
        assert requests_seen == (
            [(True, None), (False, None)]
            if failure_phase == "follow"
            else [(True, None), (False, "0"), (False, "0")]
        )
    finally:
        proxy.clear()
