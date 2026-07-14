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
from collections.abc import AsyncIterator
from types import ModuleType

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


def test_follow_reconnects_after_decompression_error(
    generated_package: ModuleType,
) -> None:
    """P2-16: a truncated gzip SSE stream (DecompressionError) is a
    broken stream: follow must probe the operation and finish/resume
    instead of crashing."""
    base = importlib.import_module("contree_client.base")
    exceptions = importlib.import_module("contree_client.exceptions")
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
            raise exceptions.DecompressionError("truncated gzip SSE")
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
    exceptions = importlib.import_module("contree_client.exceptions")
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
    with pytest.raises(exceptions.SSEStreamError) as excinfo:
        list(client.iter_operation_events("00000000-0000-0000-0000-000000000000"))
    # before the fix the cursor stayed None: the reconnect would have
    # replayed everything from the beginning
    assert excinfo.value.last_event_id == 5
