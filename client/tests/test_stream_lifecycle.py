"""Async stream lifecycle: early close must release the transport.

Regression tests for improvements.md P1-08: the public async
generators wrap ``self.stream(...)`` / ``iter_operation_events(...)``
in ``contextlib.aclosing``, so ``aclose()`` propagates immediately
instead of waiting for non-deterministic async finalization.
"""

from __future__ import annotations

import asyncio
import importlib
import json
from collections.abc import AsyncIterator, Awaitable
from types import ModuleType, SimpleNamespace

import httpx
import pytest

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
            raise EOFError("truncated gzip SSE")
            yield b""  # pragma: no cover - makes this a generator

        def close(self) -> None:
            pass

    client = TruncatedStreamClient()
    events = list(client.follow_operation_events(OPERATION_UUID))
    assert events == []  # the stream died; the terminal probe ended it
    assert client.stream_attempts == 1


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
    with pytest.raises(ConnectionError) as caught:
        list(client.iter_operation_events("00000000-0000-0000-0000-000000000000"))
    assert caught.value.__dict__["last_event_id"] == 5
