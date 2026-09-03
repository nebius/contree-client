from __future__ import annotations

import json
from types import ModuleType

import pytest


def frames_from(runtime: ModuleType, payload: bytes, chunk_size: int = 7) -> list:
    parser = runtime.SSEParser()
    frames = []
    for start in range(0, len(payload), chunk_size):
        frames.extend(parser.feed(payload[start : start + chunk_size]))
    return frames


ORDERED_LIFECYCLE = (
    b": keepalive\n"
    b"\n"
    b"id: 0\n"
    b"event: init\n"
    b'data: {"id":0,"ts":"2026-06-08T20:00:00Z","spid":0,"type":"init",'
    b'"data":{"init_pid":1}}\n'
    b"\n"
    b"id: 1\n"
    b"event: spawn\n"
    b'data: {"id":1,"ts":"2026-06-08T20:00:00.10Z","spid":1,"type":"spawn",'
    b'"data":{"pid":4242}}\n'
    b"\n"
    b"id: 2\n"
    b"event: exit\n"
    b'data: {"id":2,"ts":"2026-06-08T20:00:00.12Z","spid":1,"type":"exit",'
    b'"data":{"pid":4242,"code":0}}\n'
    b"\n"
)


def test_parser_ordered_lifecycle(runtime: ModuleType) -> None:
    frames = frames_from(runtime, ORDERED_LIFECYCLE)
    assert [f.id for f in frames] == [0, 1, 2]
    assert [f.event for f in frames] == ["init", "spawn", "exit"]
    payload = json.loads(frames[2].data)
    assert payload["data"]["code"] == 0


def test_parser_ignores_keepalive_comments(runtime: ModuleType) -> None:
    frames = frames_from(runtime, b": keepalive\n\n: keepalive\n\n")
    assert frames == []


def test_parser_crlf(runtime: ModuleType) -> None:
    frames = frames_from(runtime, b"id: 5\r\nevent: stdout\r\ndata: x\r\n\r\n")
    assert len(frames) == 1
    assert frames[0].id == 5
    assert frames[0].event == "stdout"
    assert frames[0].data == "x"


def test_parser_multiline_data(runtime: ModuleType) -> None:
    frames = frames_from(runtime, b"data: line1\ndata: line2\n\n")
    assert frames[0].data == "line1\nline2"


def test_decode_event_frame(runtime: ModuleType, models: ModuleType) -> None:
    frames = frames_from(runtime, ORDERED_LIFECYCLE)
    event = runtime.decode_event_frame(frames[0])
    assert event is not None
    assert event.type == "init"
    assert event.spid == 0


def test_sse_error_frame_raises_connection_error(runtime: ModuleType) -> None:
    frame = runtime.SSEFrame(event="sse_error", data="boom")
    with pytest.raises(ConnectionError, match="boom") as caught:
        runtime.decode_event_frame(frame, last_event_id=17)
    assert type(caught.value) is ConnectionError
    assert caught.value.__dict__["last_event_id"] == 17
