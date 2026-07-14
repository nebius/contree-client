from __future__ import annotations

import asyncio
import hashlib
import importlib
import io
import json
import ssl
import tarfile
import time
from collections.abc import Callable
from types import ModuleType
from typing import Any

import pytest

from tests import stub_server as stub
from tests.conftest import PROJECT, TOKEN, client_class
from tests.stub_server import StubServer

Invoke = Callable[..., Any]


def last_json(server: StubServer) -> dict[str, Any]:
    body: dict[str, Any] = json.loads(server.last.body)
    return body


def test_sync_context_manager(
    generated_package: ModuleType,
    stub_server: StubServer,
) -> None:
    module = importlib.import_module("contree_client.requests")
    with module.ContreeClient(
        TOKEN,
        base_url=stub_server.base_url,
        project=PROJECT,
    ) as client:
        assert client.whoami().token_uuid == stub.WHOAMI["token_uuid"]


def test_async_context_manager(
    generated_package: ModuleType,
    stub_server: StubServer,
) -> None:
    module = importlib.import_module("contree_client.aiohttp")

    async def main() -> str:
        async with module.ContreeAsyncClient(
            TOKEN,
            base_url=stub_server.base_url,
            project=PROJECT,
        ) as client:
            response = await client.whoami()
            return str(response.token_uuid)

    assert asyncio.run(main()) == stub.WHOAMI["token_uuid"]


def test_async_enter_opens_transport_eagerly(
    generated_package: ModuleType,
    stub_server: StubServer,
) -> None:
    """__aenter__ calls open(): the aiohttp session must exist right
    after entering the context, before any request is made."""
    module = importlib.import_module("contree_client.aiohttp")

    async def main() -> None:
        client = module.ContreeAsyncClient(
            TOKEN,
            base_url=stub_server.base_url,
            project=PROJECT,
        )
        assert client._session is None
        async with client:
            assert client._session is not None
            assert not client._session.closed
        assert client._session.closed

    asyncio.run(main())


def test_sync_enter_calls_open(
    generated_package: ModuleType,
    stub_server: StubServer,
) -> None:
    module = importlib.import_module("contree_client.requests")
    client = module.ContreeClient(
        TOKEN,
        base_url=stub_server.base_url,
        project=PROJECT,
    )
    opened = []
    original_open = client.open

    def tracking_open() -> None:
        opened.append(True)
        original_open()

    client.open = tracking_open  # type: ignore[method-assign]
    with client:
        assert opened == [True]


def test_accept_encoding_gzip_advertised(
    invoke: Invoke,
    stub_server: StubServer,
) -> None:
    """The API responds with gzip for everything; every backend must
    advertise gzip support and decode responses transparently (the
    stub server compresses all bodies, so the whole suite checks the
    decoding part)."""
    invoke("whoami")
    assert "gzip" in stub_server.last.headers.get("accept-encoding", "")


def test_whoami_and_auth_headers(invoke: Invoke, stub_server: StubServer) -> None:
    response = invoke("whoami")
    assert response.token_uuid == stub.WHOAMI["token_uuid"]
    assert response.token_expiration is None
    assert response.permissions["spawn"] is True
    request = stub_server.last
    assert request.headers["authorization"] == f"Bearer {TOKEN}"
    assert request.headers["project"] == PROJECT


def test_list_images_query_serialization(
    invoke: Invoke,
    stub_server: StubServer,
) -> None:
    result = invoke("list_images", limit=5, tagged=True, tag="busy", since="1h")
    assert result.images[0].uuid == stub.IMAGE_UUID
    query = stub_server.last.query
    assert query["limit"] == ["5"]
    assert query["tagged"] == ["1"]
    assert query["tag"] == ["busy"]
    assert query["since"] == ["1h"]
    assert "until" not in query


def test_list_images_bad_request(
    invoke: Invoke,
    stub_server: StubServer,
    exceptions: ModuleType,
) -> None:
    with pytest.raises(exceptions.BadRequestError) as excinfo:
        invoke("list_images", limit=0)
    assert excinfo.value.status == 400
    assert "limit" in str(excinfo.value)


def test_update_image_tag(invoke: Invoke, stub_server: StubServer) -> None:
    image = invoke("update_image_tag", stub.IMAGE_UUID, tag="busybox:custom")
    assert image.tag == "busybox:custom"
    request = stub_server.last
    assert request.method == "PATCH"
    assert request.headers["content-type"] == "application/json"
    assert last_json(stub_server) == {"tag": "busybox:custom"}


def test_delete_image_tag(invoke: Invoke, stub_server: StubServer) -> None:
    assert invoke("delete_image_tag", stub.IMAGE_UUID, tag="busybox:latest") is None
    request = stub_server.last
    assert request.method == "DELETE"
    assert request.query["tag"] == ["busybox:latest"]


def test_import_image(
    invoke: Invoke,
    stub_server: StubServer,
    models: ModuleType,
) -> None:
    registry = models.ImageImportRegistry(url="docker://docker.io/busybox:latest")
    operation_id = invoke("import_image", registry=registry, tag="busybox:latest")
    assert operation_id == stub.OPERATION_UUID
    body = last_json(stub_server)
    # timeout is unset -> omitted, server default applies; the nested
    # registry object also carries no unset "credentials" key
    assert set(body) == {"registry", "tag"}
    assert body["registry"] == {"url": "docker://docker.io/busybox:latest"}
    assert body["tag"] == "busybox:latest"


def test_upload_file(invoke: Invoke, stub_server: StubServer) -> None:
    content = b"hello"
    response = invoke("upload_file", content)
    assert response.sha256 == hashlib.sha256(content).hexdigest()
    assert response.size == len(content)
    request = stub_server.last
    assert request.body == content
    assert request.headers["content-type"] == "application/octet-stream"


def test_upload_file_like_body(invoke: Invoke, stub_server: StubServer) -> None:
    """P1-06: every backend must send a file-like body, async included
    (httpx.AsyncClient used to reject the sync body iterator)."""
    content = b"streamed payload " * 64
    response = invoke("upload_file", io.BytesIO(content))
    assert response.sha256 == hashlib.sha256(content).hexdigest()
    assert stub_server.last.body == content


def test_list_files(invoke: Invoke, stub_server: StubServer) -> None:
    result = invoke("list_files", limit=10)
    assert result.files[0].sha256 == stub.KNOWN_SHA256
    assert result.files[0].created_at.year == 2024


def test_check_file_exists(invoke: Invoke, stub_server: StubServer) -> None:
    assert invoke("check_file_exists", stub.KNOWN_SHA256) is True
    assert invoke("check_file_exists", "b" * 64) is False


def test_get_file(invoke: Invoke, stub_server: StubServer) -> None:
    info = invoke("get_file", stub.KNOWN_SHA256)
    assert info.uuid == stub.FILE_UUID


def test_spawn_instance(invoke: Invoke, stub_server: StubServer) -> None:
    response = invoke(
        "spawn_instance",
        "echo hi",
        "tag:busybox:latest",
        shell=True,
        env={"KEY": "value"},
        timeout=30,
    )
    assert response.uuid == stub.OPERATION_UUID
    body = last_json(stub_server)
    # unset parameters must be omitted entirely so the server-side
    # defaults apply; only the explicitly passed fields are sent
    assert set(body) == {"command", "image", "shell", "env", "timeout"}
    assert body["command"] == "echo hi"
    assert body["image"] == "tag:busybox:latest"
    assert body["shell"] is True
    assert body["env"] == {"KEY": "value"}
    assert body["timeout"] == 30


def test_spawn_instance_minimal_body(
    invoke: Invoke,
    stub_server: StubServer,
) -> None:
    invoke("spawn_instance", "/bin/true", "tag:busybox:latest")
    assert set(last_json(stub_server)) == {"command", "image"}


def test_spawn_instance_explicit_falsy_values_are_sent(
    invoke: Invoke,
    stub_server: StubServer,
) -> None:
    invoke(
        "spawn_instance",
        "/bin/true",
        "tag:busybox:latest",
        shell=False,
        uid=0,
        cwd="",
    )
    body = last_json(stub_server)
    assert set(body) == {"command", "image", "shell", "uid", "cwd"}
    assert body["shell"] is False
    assert body["uid"] == 0
    assert body["cwd"] == ""


def test_spawn_instance_explicit_null_is_sent(
    invoke: Invoke,
    stub_server: StubServer,
) -> None:
    invoke("spawn_instance", "/bin/true", "tag:busybox:latest", env=None)
    body = last_json(stub_server)
    assert set(body) == {"command", "image", "env"}
    assert body["env"] is None


def test_list_images_no_default_query(
    invoke: Invoke,
    stub_server: StubServer,
) -> None:
    invoke("list_images")
    assert stub_server.last.query == {}


def test_list_operations(invoke: Invoke, stub_server: StubServer) -> None:
    operations = invoke("list_operations", status="SUCCESS")
    assert len(operations) == 1
    assert operations[0].uuid == stub.OPERATION_UUID
    assert stub_server.last.query["status"] == ["SUCCESS"]


def test_get_operation_status(
    invoke: Invoke,
    stub_server: StubServer,
    models: ModuleType,
) -> None:
    operation = invoke("get_operation_status", stub.OPERATION_UUID, inflight=True)
    assert operation.status == "SUCCESS"
    assert isinstance(operation.metadata, models.OperationInstanceMetadata)
    assert operation.metadata.result.stdout.value == "aGkK"
    assert operation.result.image == stub.IMAGE_UUID
    assert stub_server.last.query["inflight"] == ["1"]


def test_cancel_operation(invoke: Invoke, stub_server: StubServer) -> None:
    assert invoke("cancel_operation", stub.OPERATION_UUID) is None


def test_cancel_operation_conflict(
    invoke: Invoke,
    stub_server: StubServer,
    exceptions: ModuleType,
) -> None:
    with pytest.raises(exceptions.ConflictError) as excinfo:
        invoke("cancel_operation", stub.CONFLICT_OPERATION_UUID)
    assert excinfo.value.status == 409


def test_iter_operation_events(
    invoke: Invoke,
    stub_server: StubServer,
    models: ModuleType,
) -> None:
    events = invoke(
        "iter_operation_events",
        stub.OPERATION_UUID,
        follow=True,
        collect=True,
    )
    assert [event.type for event in events] == ["init", "spawn", "exit"]
    assert isinstance(events[0].data, models.EventDataInit)
    assert isinstance(events[1].data, models.EventDataSpawn)
    assert events[1].data.pid == 4242
    assert isinstance(events[2].data, models.EventDataExit)
    assert events[2].data.code == 0
    request = stub_server.last
    assert request.query["follow"] == ["1"]
    assert request.headers["accept"] == "text/event-stream"


def test_iter_operation_events_gzip_sync_flush_incremental(
    invoke: Invoke,
    stub_server: StubServer,
) -> None:
    """Events must arrive before the stream ends.

    The stub compresses each SSE frame with Z_SYNC_FLUSH and then keeps
    the connection open; a client that buffers the whole gzip stream
    (or reads until EOF) hangs for SSE_HANG_SECONDS and fails the
    elapsed-time assertion.
    """
    start = time.monotonic()
    events = invoke(
        "iter_operation_events",
        stub.SLOW_OPERATION_UUID,
        follow=True,
        take=3,
    )
    elapsed = time.monotonic() - start
    assert [event.type for event in events] == ["init", "spawn", "exit"]
    assert elapsed < stub.SSE_HANG_SECONDS / 2


def test_iter_operation_events_resume_header(
    invoke: Invoke,
    stub_server: StubServer,
) -> None:
    invoke(
        "iter_operation_events",
        stub.OPERATION_UUID,
        last_event_id=1,
        collect=True,
    )
    assert stub_server.last.headers["last-event-id"] == "1"


def test_iter_operation_events_sse_error_carries_last_event_id(
    invoke: Invoke,
    stub_server: StubServer,
    exceptions: ModuleType,
) -> None:
    """The in-band `sse_error` frame maps to SSEStreamError with the id
    of the last received event, ready to be passed back as
    `last_event_id` on reconnect (spec: "retry since last event id")."""
    with pytest.raises(exceptions.SSEStreamError) as excinfo:
        invoke(
            "iter_operation_events",
            stub.BROKEN_OPERATION_UUID,
            collect=True,
        )
    assert "upstream event source closed unexpectedly" in str(excinfo.value)
    assert excinfo.value.last_event_id == 1


def test_iter_operation_events_gone(
    invoke: Invoke,
    stub_server: StubServer,
    exceptions: ModuleType,
) -> None:
    with pytest.raises(exceptions.GoneError) as excinfo:
        invoke(
            "iter_operation_events",
            stub.GONE_OPERATION_UUID,
            collect=True,
        )
    assert excinfo.value.status == 410
    assert excinfo.value.retry_after == 3


def test_inspect_find_image_by_tag(invoke: Invoke, stub_server: StubServer) -> None:
    resolved = invoke("inspect_find_image_by_tag", "busybox:latest")
    assert resolved == stub.IMAGE_UUID
    assert stub_server.last.query["tag"] == ["busybox:latest"]


def test_inspect_image(invoke: Invoke, stub_server: StubServer) -> None:
    image = invoke("inspect_image", stub.IMAGE_UUID)
    assert image.uuid == stub.IMAGE_UUID


def test_inspect_image_download(invoke: Invoke, stub_server: StubServer) -> None:
    content = invoke("inspect_image_download", stub.IMAGE_UUID, "/etc/hosts")
    assert content == stub.DOWNLOAD_CONTENT
    assert stub_server.last.query["path"] == ["/etc/hosts"]


def test_inspect_image_download_stream(
    invoke: Invoke,
    stub_server: StubServer,
) -> None:
    chunks = invoke(
        "inspect_image_download_stream",
        stub.IMAGE_UUID,
        "/etc/hosts",
        collect=True,
    )
    assert b"".join(chunks) == stub.DOWNLOAD_CONTENT


def test_inspect_image_download_not_found(
    invoke: Invoke,
    stub_server: StubServer,
    exceptions: ModuleType,
) -> None:
    with pytest.raises(exceptions.NotFoundError):
        invoke("inspect_image_download", stub.IMAGE_UUID, "/nope")


def test_check_image_file(invoke: Invoke, stub_server: StubServer) -> None:
    assert invoke("check_image_file", stub.IMAGE_UUID, "/etc/hosts") is True
    assert invoke("check_image_file", stub.IMAGE_UUID, "/nope") is False


def read_tar_names(payload: bytes) -> list[str]:
    # mode "r:*" autodetects plain vs gzip-compressed archives
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:*") as tar:
        return tar.getnames()


def test_inspect_image_archive_streams_tar(
    invoke: Invoke,
    stub_server: StubServer,
) -> None:
    chunks = invoke("inspect_image_archive", stub.IMAGE_UUID, "/etc", collect=True)
    payload = b"".join(chunks)
    assert payload == stub.ARCHIVE_CONTENT
    assert read_tar_names(payload) == ["etc/hosts", "etc/hostname"]


def test_inspect_image_archive_compressed_keeps_gzip(
    invoke: Invoke,
    stub_server: StubServer,
) -> None:
    """compressed=True skips transparent decompression: the joined
    chunks are a valid .tar.gz (the gzip the stub always applies)."""
    chunks = invoke(
        "inspect_image_archive",
        stub.IMAGE_UUID,
        "/etc",
        compressed=True,
        collect=True,
    )
    payload = b"".join(chunks)
    assert payload[:2] == b"\x1f\x8b"
    assert read_tar_names(payload) == ["etc/hosts", "etc/hostname"]


def test_inspect_image_archive_delivered_incrementally(
    invoke: Invoke,
    stub_server: StubServer,
) -> None:
    """The archive must be a real stream, not a buffered body.

    The slow route emits all chunks and then keeps the connection open
    for SSE_HANG_SECONDS; taking the first chunk must not wait for
    EOF. Closing the iterator early also exercises per-backend
    connection cleanup.
    """
    start = time.monotonic()
    chunks = invoke(
        "inspect_image_archive",
        stub.SLOW_IMAGE_UUID,
        "/etc",
        take=1,
    )
    elapsed = time.monotonic() - start
    assert chunks and chunks[0]
    assert elapsed < stub.SSE_HANG_SECONDS / 2


def test_inspect_image_archive_compressed_is_incremental_too(
    invoke: Invoke,
    stub_server: StubServer,
) -> None:
    start = time.monotonic()
    chunks = invoke(
        "inspect_image_archive",
        stub.SLOW_IMAGE_UUID,
        "/etc",
        compressed=True,
        take=1,
    )
    elapsed = time.monotonic() - start
    assert chunks and chunks[0]
    assert elapsed < stub.SSE_HANG_SECONDS / 2


def test_inspect_image_archive_not_found_raises_from_stream(
    invoke: Invoke,
    stub_server: StubServer,
    exceptions: ModuleType,
) -> None:
    with pytest.raises(exceptions.NotFoundError):
        invoke(
            "inspect_image_archive",
            stub.IMAGE_UUID,
            "/nope",
            collect=True,
        )


def test_inspect_image_download_stream_matches_content(
    invoke: Invoke,
    stub_server: StubServer,
) -> None:
    chunks = invoke(
        "inspect_image_download_stream",
        stub.IMAGE_UUID,
        "/etc/hosts",
        collect=True,
    )
    assert b"".join(chunks) == stub.DOWNLOAD_CONTENT


def test_check_image_archive(invoke: Invoke, stub_server: StubServer) -> None:
    assert invoke("check_image_archive", stub.IMAGE_UUID, "/etc") is True
    assert invoke("check_image_archive", stub.IMAGE_UUID, "/nope") is False


def test_inspect_image_list(invoke: Invoke, stub_server: StubServer) -> None:
    listing = invoke("inspect_image_list", stub.IMAGE_UUID, "/etc")
    assert listing.path == "/etc"
    assert listing.files[0].path == "passwd"
    assert listing.files[0].is_regular is True
    assert "text" not in stub_server.last.query


BAD_BASE_URLS = [
    "htps://api.example.test",  # typo would silently mean plaintext :80
    "ftp://api.example.test",
    "https://",  # no hostname
    "api.example.test",  # no scheme at all
]


@pytest.mark.parametrize("bad_url", BAD_BASE_URLS)
def test_base_url_is_validated(generated_package: ModuleType, bad_url: str) -> None:
    """P1-04: only http(s) with a hostname may build a client."""
    for module_name, class_name in (
        ("http", "ContreeClient"),
        ("httpx", "ContreeAsyncClient"),
        ("testing", "ContreeClient"),
    ):
        module = importlib.import_module(f"contree_client.{module_name}")
        with pytest.raises(ValueError, match="base_url"):
            getattr(module, class_name)("token", base_url=bad_url)


def test_base_url_accepts_http_and_https(generated_package: ModuleType) -> None:
    testing = importlib.import_module("contree_client.testing")
    testing.ContreeClient("token", base_url="http://127.0.0.1:1")
    testing.ContreeClient("token", base_url="https://api.example.test/")


def test_compressed_stream_error_body_is_decoded(
    invoke: Invoke, stub_server: StubServer, exceptions: ModuleType
) -> None:
    """P3-03: auto_decompress=False applies to the payload, not to an
    error body - the parsed server message must survive raw mode."""
    with pytest.raises(exceptions.GoneError) as excinfo:
        invoke(
            "inspect_image_archive",
            stub.GONE_IMAGE_UUID,
            "/etc",
            compressed=True,
            collect=True,
        )
    assert excinfo.value.error == "image archive is gone"


@pytest.mark.parametrize("backend", ("http", "urllib3", "requests", "httpx"))
def test_download_stream_read_timeout(
    backend: str,
    generated_package: ModuleType,
    stub_server: StubServer,
) -> None:
    """P1-07: a download whose peer stops sending must time out; only
    SSE streams are allowed to idle indefinitely."""
    started = time.monotonic()
    client = client_class(backend)(TOKEN, base_url=stub_server.base_url, timeout=1.0)
    try:
        with pytest.raises(Exception, match=r"(?i)time|read"):
            list(client.inspect_image_archive(stub.SLOW_IMAGE_UUID, "/etc"))
    finally:
        client.close()
    assert time.monotonic() - started < 5


def test_download_stream_read_timeout_async(
    generated_package: ModuleType,
    stub_server: StubServer,
) -> None:
    module = importlib.import_module("contree_client.aiohttp")

    async def scenario() -> None:
        async with module.ContreeAsyncClient(
            TOKEN, base_url=stub_server.base_url, timeout=1.0
        ) as client:
            with pytest.raises(Exception, match=r"(?i)time|read"):
                collected = [
                    chunk
                    async for chunk in client.inspect_image_archive(
                        stub.SLOW_IMAGE_UUID, "/etc"
                    )
                ]
                del collected

    started = time.monotonic()
    asyncio.run(scenario())
    assert time.monotonic() - started < 5


def test_stdlib_rejects_truncated_gzip_archive(
    generated_package: ModuleType,
    stub_server: StubServer,
    exceptions: ModuleType,
) -> None:
    """P2-16 end to end: the peer dies before the gzip trailer."""
    module = importlib.import_module("contree_client.http")
    with (
        module.ContreeClient(TOKEN, base_url=stub_server.base_url) as client,
        pytest.raises(exceptions.DecompressionError, match="truncated"),
    ):
        list(client.inspect_image_archive(stub.TRUNCATED_IMAGE_UUID, "/etc"))


def test_ssl_context_is_wired_into_every_transport(
    generated_package: ModuleType,
) -> None:
    """Every adapter accepts a caller-supplied ssl.SSLContext and hands
    it down to its transport untouched."""
    context = ssl.create_default_context()

    http_module = importlib.import_module("contree_client.http")
    with http_module.ContreeClient(
        TOKEN, base_url="https://localhost:1", ssl_context=context
    ) as client:
        assert client._connect()._context is context

    urllib3_module = importlib.import_module("contree_client.urllib3")
    with urllib3_module.ContreeClient(TOKEN, ssl_context=context) as client:
        assert client._http.connection_pool_kw["ssl_context"] is context

    requests_module = importlib.import_module("contree_client.requests")
    with requests_module.ContreeClient(TOKEN, ssl_context=context) as client:
        adapter = client._session.get_adapter("https://example.com/")
        assert isinstance(adapter, requests_module.SSLContextAdapter)
        assert adapter.poolmanager.connection_pool_kw["ssl_context"] is context

    httpx_module = importlib.import_module("contree_client.httpx")
    with httpx_module.ContreeClient(TOKEN, ssl_context=context) as client:
        assert client._client._transport._pool._ssl_context is context

    async def async_flavours() -> None:
        async with httpx_module.ContreeAsyncClient(
            TOKEN, ssl_context=context
        ) as client:
            assert client._client._transport._pool._ssl_context is context

        aiohttp_module = importlib.import_module("contree_client.aiohttp")
        async with aiohttp_module.ContreeAsyncClient(
            TOKEN, ssl_context=context
        ) as client:
            assert client._get_session().connector._ssl is context

    asyncio.run(async_flavours())


def test_ssl_context_conflicts_with_byo_transport(
    generated_package: ModuleType,
) -> None:
    """A caller-supplied transport owns its TLS configuration: passing
    ssl_context alongside it must fail loudly instead of being
    silently ignored."""
    context = ssl.create_default_context()

    urllib3_module = importlib.import_module("contree_client.urllib3")
    urllib3_library = importlib.import_module("urllib3")
    with pytest.raises(ValueError, match="urllib3_pool_manager"):
        urllib3_module.ContreeClient(
            TOKEN,
            ssl_context=context,
            urllib3_pool_manager=urllib3_library.PoolManager(),
        )

    requests_module = importlib.import_module("contree_client.requests")
    requests_library = importlib.import_module("requests")
    with (
        requests_library.Session() as session,
        pytest.raises(ValueError, match="requests_session"),
    ):
        requests_module.ContreeClient(
            TOKEN, ssl_context=context, requests_session=session
        )

    httpx_module = importlib.import_module("contree_client.httpx")
    httpx_library = importlib.import_module("httpx")
    with (
        httpx_library.Client() as httpx_client,
        pytest.raises(ValueError, match="httpx_client"),
    ):
        httpx_module.ContreeClient(
            TOKEN, ssl_context=context, httpx_client=httpx_client
        )

    async def async_flavours() -> None:
        async with httpx_library.AsyncClient() as async_client:
            with pytest.raises(ValueError, match="httpx_client"):
                httpx_module.ContreeAsyncClient(
                    TOKEN, ssl_context=context, httpx_client=async_client
                )

        aiohttp_module = importlib.import_module("contree_client.aiohttp")
        aiohttp_library = importlib.import_module("aiohttp")
        async with aiohttp_library.ClientSession() as session:
            with pytest.raises(ValueError, match="aiohttp_session"):
                aiohttp_module.ContreeAsyncClient(
                    TOKEN, ssl_context=context, aiohttp_session=session
                )

    asyncio.run(async_flavours())


def test_wait_operation_deadline_bounds_idle_sse(
    generated_package: ModuleType,
    stub_server: StubServer,
) -> None:
    """P1-05: an SSE stream that goes silent (no completion frame)
    must not outlive wait_operation(timeout=...)."""
    module = importlib.import_module("contree_client.http")
    started = time.monotonic()
    with (
        module.ContreeClient(TOKEN, base_url=stub_server.base_url) as client,
        pytest.raises(TimeoutError),
    ):
        client.wait_operation(stub.SLOW_OPERATION_UUID, timeout=0.5)
    # the stub hangs the stream for 10s: the deadline must cut in long
    # before that
    assert time.monotonic() - started < 5


def test_wait_operation_deadline_survives_keepalive_frames(
    generated_package: ModuleType,
    stub_server: StubServer,
) -> None:
    """P1-05: SSE keepalive comments reset the per-recv socket timeout,
    so only the per-chunk deadline check can bound the wait."""
    module = importlib.import_module("contree_client.http")
    started = time.monotonic()
    with (
        module.ContreeClient(TOKEN, base_url=stub_server.base_url) as client,
        pytest.raises(TimeoutError),
    ):
        client.wait_operation(stub.KEEPALIVE_OPERATION_UUID, timeout=0.3)
    # 500 keepalive frames at ~10ms each is ~5s of stream: the
    # deadline must cut in at ~0.3s
    assert time.monotonic() - started < 2
