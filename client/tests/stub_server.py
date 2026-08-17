"""A canned-response HTTP server covering the Contree API for client tests."""

from __future__ import annotations

import collections
import contextlib
import gzip
import hashlib
import io
import json
import sys
import tarfile
import threading
import time
import zlib
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

IMAGE_UUID = "12345678-9abc-baba-deda-0123456789ab"
OPERATION_UUID = "87654321-9abc-baba-deda-0123456789ab"
CONFLICT_OPERATION_UUID = "00000000-0000-0000-0000-00000000c0f1"
GONE_OPERATION_UUID = "00000000-0000-0000-0000-000000000601"
SLOW_OPERATION_UUID = "00000000-0000-0000-0000-00000000510f"
SLOW_IMAGE_UUID = "00000000-0000-0000-0000-0000000051ae"
TRUNCATED_IMAGE_UUID = "00000000-0000-0000-0000-000000000cec"
GONE_IMAGE_UUID = "00000000-0000-0000-0000-00000000e601"
BROKEN_OPERATION_UUID = "00000000-0000-0000-0000-000000000bad"
FLAKY_OPERATION_UUID = "00000000-0000-0000-0000-00000000f1a2"
RETRY_AFTER_OPERATION_UUID = "00000000-0000-0000-0000-000000000425"
PENDING_OPERATION_UUID = "00000000-0000-0000-0000-0000000e4ec1"
EXECUTING_OPERATION_UUID = "00000000-0000-0000-0000-0000000e4ec2"
RECONNECT_OPERATION_UUID = "00000000-0000-0000-0000-00000000ec0e"
PAYLOAD_INTERRUPTION_OPERATION_UUID = "00000000-0000-0000-0000-00000000badd"
PAYLOAD_TIMEOUT_OPERATION_UUID = "00000000-0000-0000-0000-00000000dead"
PAYLOAD_STALLED_STATUS_OPERATION_UUID = "00000000-0000-0000-0000-0000000057a1"
PAYLOAD_FORBIDDEN_OPERATION_UUID = "00000000-0000-0000-0000-000000000403"
PAYLOAD_FINAL_STATUS_OPERATION_UUID = "00000000-0000-0000-0000-00000000f1a1"
CLOSING_OPERATION_UUID = "00000000-0000-0000-0000-0000000c105e"
DROPPING_OPERATION_UUID = "00000000-0000-0000-0000-00000000d40b"
KEEPALIVE_OPERATION_UUID = "00000000-0000-0000-0000-0000000cee9a"
RESET_OPERATION_UUID = "00000000-0000-0000-0000-000000000e5e"
EVENTS_UNAVAILABLE_OPERATION_UUID = "00000000-0000-0000-0000-00000000eee0"
EVENTS_UNAVAILABLE_STALLED_OPERATION_UUID = "00000000-0000-0000-0000-00000000eee1"
SSE_HANG_SECONDS = 10.0
STATUS_HANG_SECONDS = 2.0
FILE_UUID = "a9165a5d-5c86-4bd8-8ee4-ae46c19cf45d"
KNOWN_SHA256 = "a" * 64
CREATED_AT = "2024-01-01T12:00:00+00:00"

IMAGE = {
    "uuid": IMAGE_UUID,
    "tag": "busybox:latest",
    "created_at": CREATED_AT,
    "operation_uuid": None,
}

PAGINATED_IMAGES = [
    {**IMAGE, "uuid": f"00000000-0000-0000-0000-00000000000{index}"}
    for index in range(5)
]

FILE_INFO = {
    "uuid": FILE_UUID,
    "sha256": KNOWN_SHA256,
    "size": 5,
    "created_at": CREATED_AT,
    "updated_at": CREATED_AT,
}

OPERATION_SUMMARY = {
    "uuid": OPERATION_UUID,
    "kind": "instance",
    "status": "SUCCESS",
    "error": None,
    "created_at": CREATED_AT,
    "duration": 1.5,
    "image_uuid": IMAGE_UUID,
    "result_image_uuid": IMAGE_UUID,
}

INSTANCE_RESULT = {
    "resources": {"elapsed_time": 0.2, "max_rss": 1024},
    "state": {"exit_code": 0, "pid": 42},
    "stdout": {"value": "aGkK", "encoding": "base64", "truncated": False},
    "stderr": {"value": "", "encoding": "ascii"},
}

OPERATION_RESPONSE = {
    "uuid": OPERATION_UUID,
    "kind": "instance",
    "status": "SUCCESS",
    "error": None,
    "created_at": CREATED_AT,
    "duration": 1.5,
    "metadata": {
        "command": "echo hi",
        "image": IMAGE_UUID,
        "shell": True,
        "result": INSTANCE_RESULT,
    },
    "result_image_uuid": IMAGE_UUID,
    "result": {"image": IMAGE_UUID, "tag": None},
}

IMPORT_OPERATION_RESPONSE = {
    "uuid": OPERATION_UUID,
    "kind": "image_import",
    "status": "SUCCESS",
    "created_at": CREATED_AT,
    "metadata": {
        "registry": {"url": "docker://docker.io/busybox:latest"},
        "tag": "busybox:latest",
        "timeout": 300,
    },
}

FILE_ITEM = {
    "size": 1024,
    "path": "passwd",
    "owner": "root",
    "group": 0,
    "uid": 0,
    "gid": 0,
    "mode": 33188,
    "mtime": 1640995200,
    "nlink": 1,
    "is_dir": False,
    "is_regular": True,
    "is_symlink": False,
    "is_socket": False,
    "is_fifo": False,
    "symlink_to": "",
}

WHOAMI = {
    "token_uuid": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
    "token_expiration": None,
    "permissions": {"spawn": True, "list": True},
    "limits": {"instance_max_timeout": 3600},
    "operations_stat": {},
}

EVENT_INIT = {
    "id": 0,
    "ts": "2026-06-08T20:00:00Z",
    "spid": 0,
    "type": "init",
    "data": {
        "started_at": "2026-06-08T20:00:00.000000000Z",
        "runtime_path": "/run/contreeinitd",
        "verbose": False,
        "init_pid": 1,
    },
}
EVENT_SPAWN = {
    "id": 1,
    "ts": "2026-06-08T20:00:00.10Z",
    "spid": 1,
    "type": "spawn",
    "data": {
        "pid": 4242,
        "command": "/bin/sh",
        "args": ["-c", "echo hi"],
        "shell": True,
        "cwd": "/",
        "uid": 0,
        "gid": 0,
        "timeout": 60,
        "truncate_at": 1048576,
        "env": {"PATH": "/usr/bin:/bin"},
    },
}
EVENT_EXIT = {
    "id": 2,
    "ts": "2026-06-08T20:00:00.12Z",
    "spid": 1,
    "type": "exit",
    "data": {
        "pid": 4242,
        "code": 0,
        "signal": -1,
        "timed_out": False,
        "duration_ms": 12,
        "resources": {
            "user_time_us": 1000,
            "sys_time_us": 500,
            "max_rss_kb": 1024,
            "shared_memory": 0,
            "unshared_memory": 0,
            "swaps": 0,
            "minor_faults": 0,
            "major_faults": 0,
            "voluntary_ctx_switches": 0,
            "involuntary_ctx_switches": 0,
            "block_input_ops": 0,
            "block_output_ops": 0,
            "ipc_msgs_sent": 0,
            "ipc_msgs_received": 0,
            "signals_received": 0,
        },
    },
}


def sse_frame(event: dict[str, Any]) -> bytes:
    return (
        f"id: {event['id']}\nevent: {event['type']}\ndata: {json.dumps(event)}\n\n"
    ).encode()


SSE_FRAMES = [
    b": keepalive\n\n",
    sse_frame(EVENT_INIT),
    sse_frame(EVENT_SPAWN),
    sse_frame(EVENT_EXIT),
]

# "broken_mid_stream" example from the spec: the stream commits to 200,
# delivers two events and then dies with an in-band sse_error frame
BROKEN_SSE_FRAMES = [
    sse_frame(EVENT_INIT),
    sse_frame(EVENT_SPAWN),
    b": stream ended with error, retry since last event id\n\n",
    b"event: sse_error\ndata: upstream event source closed unexpectedly\n\n",
]

EVENT_COMPLETION = {
    "id": 3,
    "ts": "2026-06-08T20:00:00.20Z",
    "spid": 0,
    "type": "completion",
    "data": {
        "status": "SUCCESS",
        "error": None,
        "duration": 1.5,
        "result_image": IMAGE_UUID,
    },
}

# the tail of the event log served after a reconnect: the exit event
# that followed the drop and the authoritative completion frame
RESUMED_SSE_FRAMES = [
    sse_frame(EVENT_EXIT),
    sse_frame(EVENT_COMPLETION),
]

DOWNLOAD_CONTENT = b"127.0.0.1 localhost\n"


def build_archive() -> bytes:
    """A real deterministic PAX tar, so tests can open it with tarfile."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for name, payload in (
            ("etc/hosts", DOWNLOAD_CONTENT),
            ("etc/hostname", b"linuxkit\n"),
        ):
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            info.mtime = 0
            tar.addfile(info, io.BytesIO(payload))
    return buffer.getvalue()


ARCHIVE_CONTENT = build_archive()
ARCHIVE_CHUNKS = [
    ARCHIVE_CONTENT[index : index + 1024]
    for index in range(0, len(ARCHIVE_CONTENT), 1024)
]


@dataclass
class Captured:
    method: str
    path: str
    query: dict[str, list[str]]
    headers: dict[str, str]
    body: bytes


@dataclass
class Reply:
    status: int
    body: bytes = b""
    content_type: str | None = "application/json"
    headers: dict[str, str] = field(default_factory=dict)
    # chunked streaming mode (SSE frames, archives): every chunk is
    # gzip-compressed incrementally with Z_SYNC_FLUSH, mirroring the
    # real API behaviour
    stream_chunks: list[bytes] | None = None
    hang: float = 0.0
    # simulate a peer dying mid-transfer: the gzip trailer never comes
    truncate_gzip: bool = False
    # simulate invalid HTTP chunk framing: close without the terminal
    # zero-length chunk after delivering all declared chunks
    truncate_chunked: bool = False
    # serve a normal keepalive response, then silently close the TCP
    # connection - the client only finds out when it tries to reuse it
    drop_after: bool = False
    # close the connection without writing anything at all: what a
    # stale keep-alive socket looks like to the next request on it
    drop_before: bool = False


def json_reply(status: int, payload: Any, **headers: str) -> Reply:
    return Reply(
        status=status,
        body=json.dumps(payload).encode(),
        headers=dict(headers),
    )


def route(request: Captured, attempts: collections.Counter[str]) -> Reply:
    method, path, query = request.method, request.path, request.query
    attempts[f"{method} {path}"] += 1
    attempt = attempts[f"{method} {path}"]

    if path == "/v1/whoami":
        return json_reply(200, WHOAMI)

    if path == f"/v1/operations/{FLAKY_OPERATION_UUID}" and method == "GET":
        if attempt <= 2:
            return json_reply(500, {"error": "boom", "status": 500})
        return json_reply(200, {**OPERATION_RESPONSE, "uuid": FLAKY_OPERATION_UUID})

    if path == f"/v1/operations/{RETRY_AFTER_OPERATION_UUID}" and method == "GET":
        if attempt == 1:
            return json_reply(
                425,
                {"error": "come back later", "status": 425},
                **{"Retry-After": "0"},
            )
        return json_reply(
            200, {**OPERATION_RESPONSE, "uuid": RETRY_AFTER_OPERATION_UUID}
        )

    if path == f"/v1/operations/{PENDING_OPERATION_UUID}" and method == "GET":
        status = "EXECUTING" if attempt <= 1 else "SUCCESS"
        return json_reply(
            200,
            {**OPERATION_RESPONSE, "uuid": PENDING_OPERATION_UUID, "status": status},
        )

    if path == f"/v1/operations/{PENDING_OPERATION_UUID}/events":
        # not ready on the first connect (425 asks to come back), the
        # retry serves the log tail ending with the completion frame
        if attempt == 1:
            return json_reply(
                425,
                {"error": "events are not ready yet", "status": 425},
                **{"Retry-After": "0"},
            )
        return Reply(
            status=200,
            content_type="text/event-stream",
            stream_chunks=RESUMED_SSE_FRAMES,
        )

    if path == f"/v1/operations/{EXECUTING_OPERATION_UUID}/events":
        # commits to 200 and ends immediately without a completion
        # frame: the operation just keeps executing
        return Reply(
            status=200,
            content_type="text/event-stream",
            stream_chunks=[b": keepalive\n\n"],
        )

    if path == f"/v1/operations/{EXECUTING_OPERATION_UUID}" and method == "GET":
        return json_reply(
            200,
            {
                **OPERATION_RESPONSE,
                "uuid": EXECUTING_OPERATION_UUID,
                "status": "EXECUTING",
            },
        )

    if path == f"/v1/operations/{DROPPING_OPERATION_UUID}" and method == "GET":
        # keepalive headers, but the server drops the connection after
        reply = json_reply(200, {**OPERATION_RESPONSE, "uuid": DROPPING_OPERATION_UUID})
        reply.drop_after = True
        return reply

    if path == f"/v1/operations/{CLOSING_OPERATION_UUID}" and method == "GET":
        # the server serves the response and closes the connection
        return json_reply(
            200,
            {**OPERATION_RESPONSE, "uuid": CLOSING_OPERATION_UUID},
            Connection="close",
        )

    if path == f"/v1/operations/{RECONNECT_OPERATION_UUID}" and method == "GET":
        return json_reply(
            200,
            {
                **OPERATION_RESPONSE,
                "uuid": RECONNECT_OPERATION_UUID,
                "status": "EXECUTING",
            },
        )

    if path == f"/v1/operations/{RECONNECT_OPERATION_UUID}/events":
        # first connection dies mid-stream with an sse_error frame;
        # the reconnect (Last-Event-Id) serves the log tail
        chunks = BROKEN_SSE_FRAMES if attempt == 1 else RESUMED_SSE_FRAMES
        return Reply(
            status=200,
            content_type="text/event-stream",
            stream_chunks=chunks,
        )

    if (
        path == f"/v1/operations/{EVENTS_UNAVAILABLE_OPERATION_UUID}"
        and method == "GET"
    ):
        # the operation itself progresses and finishes normally; only
        # its /events route is missing
        status = "EXECUTING" if attempt < 3 else "SUCCESS"
        return json_reply(
            200,
            {
                **OPERATION_RESPONSE,
                "uuid": EVENTS_UNAVAILABLE_OPERATION_UUID,
                "status": status,
            },
        )

    if path == f"/v1/operations/{EVENTS_UNAVAILABLE_OPERATION_UUID}/events":
        # no /events route at all for this operation (older backend,
        # or a reverse proxy that doesn't forward it): reconnecting
        # this exact request will never succeed, but the operation
        # itself is healthy - the client must fall back to polling
        # get_operation_status instead of failing the whole wait
        return json_reply(404, {"error": "not found", "status": 404})

    if (
        path == f"/v1/operations/{EVENTS_UNAVAILABLE_STALLED_OPERATION_UUID}"
        and method == "GET"
    ):
        # same missing-events situation, but the operation itself
        # never finishes - the polling fallback must still honor the
        # caller's deadline instead of looping forever
        return json_reply(
            200,
            {
                **OPERATION_RESPONSE,
                "uuid": EVENTS_UNAVAILABLE_STALLED_OPERATION_UUID,
                "status": "EXECUTING",
            },
        )

    if path == f"/v1/operations/{EVENTS_UNAVAILABLE_STALLED_OPERATION_UUID}/events":
        return json_reply(404, {"error": "not found", "status": 404})

    if (
        path == f"/v1/operations/{PAYLOAD_INTERRUPTION_OPERATION_UUID}"
        and method == "GET"
    ):
        status = "EXECUTING" if attempt == 1 else "SUCCESS"
        return json_reply(
            200,
            {
                **OPERATION_RESPONSE,
                "uuid": PAYLOAD_INTERRUPTION_OPERATION_UUID,
                "status": status,
            },
        )

    if path == f"/v1/operations/{PAYLOAD_INTERRUPTION_OPERATION_UUID}/events":
        if attempt == 1:
            return Reply(
                status=200,
                content_type="text/event-stream",
                stream_chunks=[sse_frame(EVENT_INIT), sse_frame(EVENT_SPAWN)],
                truncate_chunked=True,
            )
        return Reply(
            status=200,
            content_type="text/event-stream",
            stream_chunks=RESUMED_SSE_FRAMES,
        )

    if path == f"/v1/operations/{PAYLOAD_TIMEOUT_OPERATION_UUID}" and method == "GET":
        return json_reply(
            200,
            {
                **OPERATION_RESPONSE,
                "uuid": PAYLOAD_TIMEOUT_OPERATION_UUID,
                "status": "EXECUTING",
            },
        )

    if path == f"/v1/operations/{PAYLOAD_TIMEOUT_OPERATION_UUID}/events":
        chunks = [sse_frame(EVENT_INIT)] if attempt == 1 else [b": keepalive\n\n"]
        return Reply(
            status=200,
            content_type="text/event-stream",
            stream_chunks=chunks,
            truncate_chunked=True,
        )

    if (
        path == f"/v1/operations/{PAYLOAD_STALLED_STATUS_OPERATION_UUID}"
        and method == "GET"
    ):
        time.sleep(STATUS_HANG_SECONDS)
        return json_reply(
            200,
            {
                **OPERATION_RESPONSE,
                "uuid": PAYLOAD_STALLED_STATUS_OPERATION_UUID,
                "status": "EXECUTING",
            },
        )

    if path == f"/v1/operations/{PAYLOAD_STALLED_STATUS_OPERATION_UUID}/events":
        return Reply(
            status=200,
            content_type="text/event-stream",
            stream_chunks=[b": keepalive\n\n"],
            truncate_chunked=True,
        )

    if path == f"/v1/operations/{PAYLOAD_FORBIDDEN_OPERATION_UUID}/events":
        return Reply(
            status=403,
            content_type="application/json",
            stream_chunks=[b'{"error":"forbidden","status":403}'],
            truncate_chunked=True,
        )

    if (
        path == f"/v1/operations/{PAYLOAD_FINAL_STATUS_OPERATION_UUID}"
        and method == "GET"
    ):
        if attempt > 1:
            time.sleep(STATUS_HANG_SECONDS)
        return json_reply(
            200,
            {
                **OPERATION_RESPONSE,
                "uuid": PAYLOAD_FINAL_STATUS_OPERATION_UUID,
                "status": "SUCCESS",
            },
        )

    if path == f"/v1/operations/{PAYLOAD_FINAL_STATUS_OPERATION_UUID}/events":
        return Reply(
            status=200,
            content_type="text/event-stream",
            stream_chunks=[b": keepalive\n\n"],
            truncate_chunked=True,
        )

    if path == "/v1/images" and method == "GET":
        if query.get("limit") == ["0"]:
            return json_reply(
                400,
                {"error": "limit must be between 1 and 1000", "status": 400},
            )
        if query.get("tag") == ["paginated"]:
            # a 5-record dataset honoring offset/limit for iterator tests
            offset = int(query.get("offset", ["0"])[0])
            limit = int(query.get("limit", ["1000"])[0])
            return json_reply(
                200,
                {"images": PAGINATED_IMAGES[offset : offset + limit]},
            )
        return json_reply(200, {"images": [IMAGE]})

    if path == f"/v1/images/{IMAGE_UUID}/tag":
        if method == "PATCH":
            payload = json.loads(request.body)
            return json_reply(200, {**IMAGE, "tag": payload["tag"]})
        if method == "DELETE":
            return Reply(status=204, content_type=None)

    if path == "/v1/images/import" and method == "POST":
        return json_reply(
            201,
            {"uuid": OPERATION_UUID},
            Location=f"/v1/operations/{OPERATION_UUID}",
        )

    if path == "/v1/files" and method == "POST":
        sha256 = hashlib.sha256(request.body).hexdigest()
        return json_reply(
            201,
            {"uuid": FILE_UUID, "sha256": sha256, "size": len(request.body)},
        )

    if path == "/v1/files" and method == "GET":
        return json_reply(200, {"files": [FILE_INFO]})

    if path == f"/v1/files/{KNOWN_SHA256}":
        if method == "HEAD":
            return Reply(status=200, content_type=None)
        return json_reply(200, FILE_INFO)

    if path.startswith("/v1/files/") and method == "HEAD":
        return Reply(status=404, content_type=None)

    if path == "/v1/instances" and method == "POST":
        payload = json.loads(request.body)
        if payload.get("command") == "flaky":
            # for retry-safety tests: the first attempt dies with 500
            attempts["flaky-spawn"] += 1
            if attempts["flaky-spawn"] == 1:
                return json_reply(500, {"error": "boom", "status": 500})
        if payload.get("command") == "flaky-425":
            # 425/429 are the backend's always-safe-to-replay statuses
            # even for a POST - see RetryPolicy.statuses
            attempts["flaky-425-spawn"] += 1
            if attempts["flaky-425-spawn"] == 1:
                return json_reply(425, {"error": "too early", "status": 425})
        if payload.get("command") == "flaky-429":
            attempts["flaky-429-spawn"] += 1
            if attempts["flaky-429-spawn"] == 1:
                return json_reply(429, {"error": "slow down", "status": 429})
        return json_reply(
            201,
            {"uuid": OPERATION_UUID, **payload},
            Location=f"/v1/operations/{OPERATION_UUID}",
        )

    if path == "/v1/operations" and method == "GET":
        return json_reply(200, [OPERATION_SUMMARY])

    if path == f"/v1/operations/{OPERATION_UUID}":
        if method == "GET":
            return json_reply(200, OPERATION_RESPONSE, **{"Retry-After": "1"})
        if method == "DELETE":
            return Reply(status=202, content_type=None)

    if path == f"/v1/operations/{CONFLICT_OPERATION_UUID}" and method == "DELETE":
        return json_reply(
            409,
            {"error": "operation is not cancellable", "status": 409},
        )

    if path == f"/v1/operations/{OPERATION_UUID}/import":
        return json_reply(200, IMPORT_OPERATION_RESPONSE)

    if path == f"/v1/operations/{OPERATION_UUID}/events":
        return Reply(
            status=200,
            content_type="text/event-stream",
            stream_chunks=SSE_FRAMES,
        )

    if path == f"/v1/operations/{RESET_OPERATION_UUID}/events":
        # the first connect lands on a "stale" socket that the server
        # closes without a byte; a retried connect gets the stream
        if attempt == 1:
            return Reply(status=200, drop_before=True)
        return Reply(
            status=200,
            content_type="text/event-stream",
            stream_chunks=SSE_FRAMES,
        )

    if path == f"/v1/operations/{BROKEN_OPERATION_UUID}/events":
        return Reply(
            status=200,
            content_type="text/event-stream",
            stream_chunks=BROKEN_SSE_FRAMES,
        )

    if path == f"/v1/operations/{KEEPALIVE_OPERATION_UUID}/events":
        # a stream that never says anything useful but keeps the
        # connection warm with comment frames - the per-recv socket
        # timeout never fires here, only an absolute deadline can
        return Reply(
            status=200,
            content_type="text/event-stream",
            stream_chunks=[b": keepalive\n\n"] * 500,
        )

    if path == f"/v1/operations/{SLOW_OPERATION_UUID}/events":
        # emits everything, then keeps the connection open: a client
        # that fails to decode gzip incrementally will hang here
        return Reply(
            status=200,
            content_type="text/event-stream",
            stream_chunks=SSE_FRAMES,
            hang=SSE_HANG_SECONDS,
        )

    if path == f"/v1/operations/{GONE_OPERATION_UUID}/events":
        return json_reply(
            410,
            {"error": "operation finished; events not yet durable", "status": 410},
            **{"Retry-After": "3"},
        )

    if path == "/v1/inspect/" and method == "GET":
        if query.get("tag"):
            # the real server sends the Location relative to /v1/inspect/
            return Reply(
                status=302,
                content_type=None,
                headers={"Location": f"{IMAGE_UUID}/"},
            )
        return json_reply(400, {"error": "tag is required", "status": 400})

    if path == f"/v1/inspect/{IMAGE_UUID}/":
        return json_reply(200, IMAGE)

    if path == f"/v1/inspect/{IMAGE_UUID}/download":
        if query.get("path") != ["/etc/hosts"]:
            return (
                Reply(status=404, content_type=None)
                if method == "HEAD"
                else json_reply(404, {"error": "file not found", "status": 404})
            )
        if method == "HEAD":
            return Reply(status=200, content_type=None)
        return Reply(
            status=200,
            body=DOWNLOAD_CONTENT,
            content_type="application/octet-stream",
        )

    if path == f"/v1/inspect/{IMAGE_UUID}/archive":
        if method == "HEAD":
            status = 200 if query.get("path") == ["/etc"] else 404
            return Reply(status=status, content_type=None)
        if query.get("path") != ["/etc"]:
            return json_reply(404, {"error": "path not found", "status": 404})
        return Reply(
            status=200,
            content_type="application/x-tar",
            stream_chunks=ARCHIVE_CHUNKS,
        )

    if path == f"/v1/inspect/{TRUNCATED_IMAGE_UUID}/archive":
        return Reply(
            status=200,
            content_type="application/x-tar",
            stream_chunks=ARCHIVE_CHUNKS[:1],
            truncate_gzip=True,
        )

    if path == f"/v1/inspect/{GONE_IMAGE_UUID}/archive":
        # a compressed error body on a streaming endpoint: raw-mode
        # clients must still decode it to surface the server message
        return json_reply(
            410,
            {"error": "image archive is gone", "status": 410},
        )

    if path == f"/v1/inspect/{SLOW_IMAGE_UUID}/archive":
        # emits everything, then keeps the connection open: a client
        # buffering the stream until EOF will hang here
        return Reply(
            status=200,
            content_type="application/x-tar",
            stream_chunks=ARCHIVE_CHUNKS,
            hang=SSE_HANG_SECONDS,
        )

    if path == f"/v1/inspect/{IMAGE_UUID}/list":
        return json_reply(200, {"path": "/etc", "files": [FILE_ITEM]})

    return json_reply(404, {"error": f"no route for {method} {path}", "status": 404})


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    stub: StubServer

    def setup(self) -> None:
        # one handler instance per TCP connection: counting them lets
        # tests assert that pooled backends reuse keepalive connections
        super().setup()
        self.stub.connection_count += 1

    def log_message(self, fmt: str, *args: Any) -> None:
        pass

    def read_chunked_body(self) -> bytes:
        # file-like uploads arrive chunked (the client streams them
        # instead of buffering the whole payload in memory)
        body = bytearray()
        while True:
            size_line = self.rfile.readline().strip()
            size = int(size_line.split(b";")[0], 16)
            if size == 0:
                self.rfile.readline()  # the trailing CRLF
                return bytes(body)
            body += self.rfile.read(size)
            self.rfile.readline()  # the chunk CRLF

    def handle_request_for(self, method: str) -> None:
        parts = urlsplit(self.path)
        if (self.headers.get("Transfer-Encoding") or "").lower() == "chunked":
            body = self.read_chunked_body()
        else:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
        request = Captured(
            method=method,
            path=parts.path,
            query=parse_qs(parts.query),
            headers={k.lower(): v for k, v in self.headers.items()},
            body=body,
        )
        self.stub.captured.append(request)
        reply = route(request, self.stub.attempts)
        if reply.drop_before:
            self.close_connection = True
            return
        if reply.stream_chunks is not None:
            self.send_stream(reply)
            return
        # The real API compresses every response, including streams and
        # errors - mimic that so the tests prove all backends decode
        # gzip transparently.
        payload = reply.body
        if payload:
            payload = gzip.compress(payload)
        self.send_response(reply.status)
        if reply.content_type is not None:
            self.send_header("Content-Type", reply.content_type)
        if payload:
            self.send_header("Content-Encoding", "gzip")
        for name, value in reply.headers.items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if method != "HEAD" and payload:
            with contextlib.suppress(BrokenPipeError, ConnectionResetError, OSError):
                self.wfile.write(payload)
        if reply.drop_after:
            self.close_connection = True

    def write_chunk(self, data: bytes) -> None:
        self.wfile.write(f"{len(data):x}\r\n".encode() + data + b"\r\n")

    def send_stream(self, reply: Reply) -> None:
        """Stream body chunks as a chunked gzip response.

        Each chunk is compressed incrementally and flushed with
        Z_SYNC_FLUSH, exactly like the real API - clients must decode
        the stream incrementally to see data before EOF.
        """
        assert reply.stream_chunks is not None
        self.send_response(reply.status)
        self.send_header("Content-Type", reply.content_type or "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Encoding", "gzip")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        compressor = zlib.compressobj(6, zlib.DEFLATED, 16 + zlib.MAX_WBITS)
        try:
            for frame in reply.stream_chunks:
                chunk = compressor.compress(frame)
                chunk += compressor.flush(zlib.Z_SYNC_FLUSH)
                self.write_chunk(chunk)
                self.wfile.flush()
                time.sleep(0.01)
            if reply.hang:
                time.sleep(reply.hang)
            if not reply.truncate_chunked:
                if not reply.truncate_gzip:
                    self.write_chunk(compressor.flush(zlib.Z_FINISH))
                self.write_chunk(b"")
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            self.close_connection = True

    def do_GET(self) -> None:
        self.handle_request_for("GET")

    def do_POST(self) -> None:
        self.handle_request_for("POST")

    def do_PATCH(self) -> None:
        self.handle_request_for("PATCH")

    def do_DELETE(self) -> None:
        self.handle_request_for("DELETE")

    def do_HEAD(self) -> None:
        self.handle_request_for("HEAD")


class StubServer:
    def __init__(self) -> None:
        self.captured: list[Captured] = []
        self.attempts: collections.Counter[str] = collections.Counter()
        self.connection_count = 0
        handler = type("BoundHandler", (Handler,), {"stub": self})
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.httpd.daemon_threads = True
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        host, port = self.httpd.server_address[:2]
        return f"http://{host}:{port}"

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()

    @property
    def last(self) -> Captured:
        return self.captured[-1]


def main() -> None:
    """Serve the stub standalone for out-of-process test suites.

    Prints the base URL as the first stdout line, then runs until
    stdin closes - the natural lifetime of a child process whose
    parent (`node --test`) exits or kills it.
    """
    server = StubServer()
    server.start()
    print(server.base_url, flush=True)  # noqa: T201 - the process contract
    with contextlib.suppress(KeyboardInterrupt):
        sys.stdin.read()
    server.stop()


if __name__ == "__main__":
    main()
