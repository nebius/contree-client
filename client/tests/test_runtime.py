from __future__ import annotations

import asyncio
import gzip
import hashlib
import io
import json
import zlib
from datetime import datetime, timezone
from types import ModuleType

import pytest


def response(runtime: ModuleType, status: int, body: bytes = b"", **headers: str):
    return runtime.ResponseData(
        status=status,
        headers={k.lower(): v for k, v in headers.items()},
        body=body,
    )


def test_format_time_param(runtime: ModuleType) -> None:
    moment = datetime(2025, 1, 1, 12, 0, tzinfo=timezone.utc)
    assert runtime.format_time_param(moment) == "2025-01-01T12:00:00+00:00"
    assert runtime.format_time_param(3600) == "3600"
    assert runtime.format_time_param("1h") == "1h"


def test_encode_query_preserves_repeated_values(runtime: ModuleType) -> None:
    assert (
        runtime.encode_query(
            {
                "pattern": ["^root:", "^bin:"],
                "path": ("/etc/passwd", "/etc/group"),
                "case": "sensitive",
            }
        )
        == "pattern=%5Eroot%3A&pattern=%5Ebin%3A"
        "&path=/etc/passwd&path=/etc/group&case=sensitive"
    )


def test_remaining_timeout_uses_shared_deadline_message(
    runtime: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runtime, "monotonic", lambda: 10.0)

    assert runtime.remaining_timeout(11.0, 2.0) == 1.0
    with pytest.raises(TimeoutError) as caught:
        runtime.remaining_timeout(10.0, None)
    assert str(caught.value) == runtime.REQUEST_DEADLINE_MESSAGE


def test_json_object_rejects_array(runtime: ModuleType) -> None:
    with pytest.raises(TypeError, match="expected a JSON object"):
        runtime.json_object(response(runtime, 200, b"[1, 2]"))


def test_json_array_rejects_object(runtime: ModuleType) -> None:
    with pytest.raises(TypeError, match="expected a JSON array"):
        runtime.json_array(response(runtime, 200, b"{}"))


def test_error_for_response_plain_text_body(
    runtime: ModuleType,
    exceptions: ModuleType,
) -> None:
    error = runtime.error_for_response(404, {}, b"nothing here")
    assert isinstance(error, exceptions.NotFoundError)
    assert error.error == "nothing here"
    assert error.retry_after is None


def test_error_for_response_invalid_retry_after(runtime: ModuleType) -> None:
    error = runtime.error_for_response(410, {"retry-after": "later"}, b"{}")
    assert error.retry_after is None


def test_redact_json_nested(runtime: ModuleType) -> None:
    payload = {
        "registry": {
            "url": "docker://x",
            "credentials": {"username": "u", "password": "p"},
        },
        "items": [{"api_key": "k", "name": "ok"}],
        "Token": "t",
        "token_uuid": "not-a-secret",
    }
    redacted = runtime.redact_json(payload)
    assert redacted["registry"]["credentials"] == "<redacted>"
    assert redacted["items"][0]["api_key"] == "<redacted>"
    assert redacted["items"][0]["name"] == "ok"
    assert redacted["Token"] == "<redacted>"
    assert redacted["token_uuid"] == "not-a-secret"
    assert redacted["registry"]["url"] == "docker://x"


def test_body_formatter_redacts_json(runtime: ModuleType) -> None:
    rendered = str(
        runtime.BodyFormatter(
            b'{"password": "p", "name": "n"}',
            "application/json",
        )
    )
    assert rendered != "p"
    assert '"password": "<redacted>"' in rendered
    assert '"name": "n"' in rendered


def test_body_formatter_never_echoes_unparsable_json(
    runtime: ModuleType,
) -> None:
    rendered = str(runtime.BodyFormatter(b"password=secret&x=1", "application/json"))
    assert "secret" not in rendered
    assert rendered == "<unparsable body 19B>"


def test_request_content_passthrough(runtime: ModuleType) -> None:
    assert runtime.request_content(None) is None
    assert runtime.request_content(b"data") == b"data"


def test_request_content_file_like(runtime: ModuleType) -> None:
    chunks = list(runtime.request_content(io.BytesIO(b"x" * 100)))
    assert b"".join(chunks) == b"x" * 100


def test_sse_parser_non_integer_id(runtime: ModuleType) -> None:
    parser = runtime.SSEParser()
    frames = parser.feed(b"id: abc\nevent: stdout\ndata: x\n\n")
    assert frames[0].id is None
    assert frames[0].event == "stdout"


def test_decode_event_frame_without_data(runtime: ModuleType) -> None:
    frame = runtime.SSEFrame(id=1, event="stdout", data="")
    assert runtime.decode_event_frame(frame) is None


def test_decode_event_frame_non_object_payload(runtime: ModuleType) -> None:
    frame = runtime.SSEFrame(id=1, event="stdout", data='"just a string"')
    assert runtime.decode_event_frame(frame) is None


def test_library_version_metadata_first(runtime: ModuleType) -> None:
    fake = ModuleType("pytest")
    fake.__version__ = "0.0.0-shadowed"

    # package metadata wins over the module's __version__
    token = runtime.library_version(fake)
    assert token.startswith("pytest/")
    assert token != "pytest/0.0.0-shadowed"


def test_library_version_dunder_fallback(runtime: ModuleType) -> None:
    # not an installed distribution -> the module's __version__
    fake = ModuleType("definitely-not-installed-lib")
    fake.__version__ = "1.2.3"

    assert runtime.library_version(fake) == "definitely-not-installed-lib/1.2.3"


def test_library_version_unknown_fallback(runtime: ModuleType) -> None:
    fake = ModuleType("definitely-not-installed-lib")

    assert runtime.library_version(fake) == "definitely-not-installed-lib/unknown"


def test_distribution_version_editable(
    runtime: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FakeDist:
        version = "1.0"

        def read_text(self, name: str) -> str:
            assert name == "direct_url.json"
            return '{"dir_info": {"editable": true}}'

    monkeypatch.setattr(runtime, "distribution", lambda name: FakeDist())
    assert runtime.distribution_version("anything") == "editable"


def test_distribution_version_regular(
    runtime: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FakeDist:
        version = "2.5"

        def read_text(self, name: str) -> str | None:
            return None

    monkeypatch.setattr(runtime, "distribution", lambda name: FakeDist())
    assert runtime.distribution_version("anything") == "2.5"


def test_file_sha256_hashes_from_current_position(runtime: ModuleType) -> None:
    """P1-09: hash must cover exactly the bytes a send would transmit."""
    stream = io.BytesIO(b"abcdef")
    stream.seek(3)

    digest = runtime.file_sha256(stream)

    assert digest == hashlib.sha256(b"def").hexdigest()
    # and the position is restored, not reset to zero
    assert stream.tell() == 3
    assert stream.read() == b"def"


def test_body_start_and_rewind_restore_offset(runtime: ModuleType) -> None:
    stream = io.BytesIO(b"abcdef")
    stream.seek(2)
    spec = runtime.RequestSpec(method="POST", path="/files", body=stream)

    start = runtime.body_start(spec)
    assert start == 2
    stream.read()  # the failed attempt consumed the body
    runtime.rewind_body(spec, start)
    assert stream.read() == b"cdef"


def test_rewind_body_rejects_non_seekable(runtime: ModuleType) -> None:
    class NonSeekable(io.BytesIO):
        def seekable(self) -> bool:
            return False

    spec = runtime.RequestSpec(method="POST", path="/files", body=NonSeekable(b"x"))
    assert runtime.body_start(spec) is None
    with pytest.raises(Exception, match="not seekable"):
        runtime.rewind_body(spec, None)


def test_async_request_content_reads_file_like(runtime: ModuleType) -> None:
    """P1-06: async transports must receive an async body iterator."""

    async def collect() -> bytes:
        source = runtime.async_request_content(io.BytesIO(b"hello world"))
        return b"".join([chunk async for chunk in source])

    assert asyncio.run(collect()) == b"hello world"
    assert runtime.async_request_content(None) is None
    assert runtime.async_request_content(b"raw") == b"raw"


def test_truncated_gzip_stream_is_rejected(
    runtime: ModuleType, exceptions: ModuleType
) -> None:
    """P2-16: a stream cut before the gzip trailer must raise, not
    pass as a complete payload."""
    compressor = zlib.compressobj(6, zlib.DEFLATED, 16 + zlib.MAX_WBITS)
    partial = compressor.compress(b"x" * 4096)
    partial += compressor.flush(zlib.Z_SYNC_FLUSH)
    # ... and the connection dies here: no Z_FINISH trailer

    decoder = runtime.stream_decoder("gzip")
    decoder.decompress(partial)
    with pytest.raises(EOFError, match="truncated"):
        decoder.flush()


def test_complete_gzip_stream_flushes_clean(runtime: ModuleType) -> None:
    decoder = runtime.stream_decoder("gzip")
    body = decoder.decompress(gzip.compress(b"payload")) + decoder.flush()
    assert body == b"payload"


def test_sse_parser_buffer_limit(runtime: ModuleType) -> None:
    """P2-17: a peer streaming an endless line must not grow the
    buffer unbounded."""
    parser = runtime.SSEParser()
    with pytest.raises(ValueError, match="exceeds"):
        parser.feed(b"x" * (parser.MAX_BUFFER + 1))


def test_decode_event_frame_exposes_malformed_json(runtime: ModuleType) -> None:
    frame = runtime.SSEFrame(id=7, event="stdout", data="{broken")
    with pytest.raises(json.JSONDecodeError):
        runtime.decode_event_frame(frame)


def test_sse_parser_caps_accumulated_event(runtime: ModuleType) -> None:
    """P2-16: many short, individually valid data lines must not grow
    the pending frame unboundedly - the cap covers the whole event."""
    parser = runtime.SSEParser()
    line = b"data: " + b"x" * 1024 + b"\n"
    with pytest.raises(ValueError, match="exceeds"):
        for _ in range(2 * parser.MAX_BUFFER // len(line) + 2):
            parser.feed(line)


def test_decode_event_frame_exposes_model_errors(runtime: ModuleType) -> None:
    frame = runtime.SSEFrame(id=8, event="stdout", data='{"unexpected": true}')
    with pytest.raises(KeyError):
        runtime.decode_event_frame(frame)


def test_body_formatter_marks_truncated_text(runtime: ModuleType) -> None:
    """P3: a long text body must carry the truncation marker instead
    of being silently cut at max_size."""
    formatter = runtime.BodyFormatter(
        b"x" * 5000, content_type="text/plain", max_size=100
    )
    rendered = str(formatter)
    assert "truncated" in rendered
    assert "5000B" in rendered
    assert len(rendered) < 200
