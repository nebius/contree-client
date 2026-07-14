"""The ensure_file deduplicating upload helper."""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import io
from types import ModuleType

PAYLOAD = b"hello world\n"
SHA = hashlib.sha256(PAYLOAD).hexdigest()


def make_double(generated_package: ModuleType, *, known: bool) -> object:
    testing = importlib.import_module("contree_client.testing")
    models = importlib.import_module("contree_client.models")
    exceptions = importlib.import_module("contree_client.exceptions")
    client = testing.ContreeClient()
    file_info = {
        "uuid": "a9165a5d-5c86-4bd8-8ee4-ae46c19cf45d",
        "sha256": SHA,
        "size": len(PAYLOAD),
        "created_at": "2024-01-01T12:00:00+00:00",
        "updated_at": "2024-01-01T12:00:00+00:00",
    }
    if known:
        client.mock("get_file", models.File.from_dict(file_info))
    else:
        client.mock("get_file", error=exceptions.NotFoundError(404, "no such file"))
    client.mock(
        "upload_file",
        models.FileResponse.from_dict(
            {"uuid": file_info["uuid"], "sha256": SHA, "size": len(PAYLOAD)}
        ),
    )
    return client


def test_ensure_file_skips_upload_when_known(generated_package: ModuleType) -> None:
    client = make_double(generated_package, known=True)

    stored = client.ensure_file(PAYLOAD)

    assert stored.sha256 == SHA
    (probe,) = client.calls_for("get_file")
    assert probe.args == (SHA,)
    assert not client.calls_for("upload_file")


def test_ensure_file_uploads_on_miss(generated_package: ModuleType) -> None:
    client = make_double(generated_package, known=False)

    stored = client.ensure_file(io.BytesIO(PAYLOAD))

    assert stored.sha256 == SHA
    assert client.calls_for("get_file")
    (upload,) = client.calls_for("upload_file")
    # the probe rewound the stream, the upload sees it from the start
    assert upload.args[0].read() == PAYLOAD


def test_ensure_file_caller_provided_sha(generated_package: ModuleType) -> None:
    client = make_double(generated_package, known=True)

    client.ensure_file(PAYLOAD, sha256="f" * 64)

    (probe,) = client.calls_for("get_file")
    # the caller-provided digest is trusted, nothing is hashed locally
    assert probe.args == ("f" * 64,)


def test_ensure_file_non_seekable_uploads_directly(
    generated_package: ModuleType,
) -> None:
    class NonSeekable(io.BytesIO):
        def seekable(self) -> bool:
            return False

    client = make_double(generated_package, known=True)

    client.ensure_file(NonSeekable(PAYLOAD))

    assert not client.calls_for("get_file")
    assert client.calls_for("upload_file")


def test_ensure_file_async(generated_package: ModuleType) -> None:
    testing = importlib.import_module("contree_client.testing")
    models = importlib.import_module("contree_client.models")
    client = testing.ContreeAsyncClient()
    client.mock(
        "get_file",
        models.File.from_dict(
            {
                "uuid": "a9165a5d-5c86-4bd8-8ee4-ae46c19cf45d",
                "sha256": SHA,
                "size": len(PAYLOAD),
                "created_at": "2024-01-01T12:00:00+00:00",
                "updated_at": "2024-01-01T12:00:00+00:00",
            }
        ),
    )

    stored = asyncio.run(client.ensure_file(PAYLOAD))

    assert stored.sha256 == SHA
    (probe,) = client.calls_for("get_file")
    assert probe.args == (SHA,)


def test_ensure_file_offset_stream_consistency(
    generated_package: ModuleType,
) -> None:
    """P1-09: the probe digest and the upload cover the same bytes."""
    client = make_double(generated_package, known=False)
    stream = io.BytesIO(b"skip" + PAYLOAD)
    stream.seek(4)  # the caller deliberately starts mid-stream

    client.ensure_file(stream)

    (probe,) = client.calls_for("get_file")
    assert probe.args == (SHA,)  # sha of PAYLOAD, not of the whole stream
    (upload,) = client.calls_for("upload_file")
    assert upload.args[0].read() == PAYLOAD
