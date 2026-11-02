from __future__ import annotations

import asyncio

import httpx
import pytest

from contree_client.httpx import ContreeAsyncClient, ContreeClient
from contree_client.operations import build_inspect_find_image_by_tag
from tests import stub_server as stub
from tests.conftest import PROJECT, TOKEN
from tests.stub_server import StubServer


@pytest.mark.parametrize("operation", ["request", "stream", "lookup"])
def test_supplied_httpx_client_does_not_follow_redirects(
    stub_server: StubServer, operation: str
) -> None:
    responses: list[httpx.Response] = []
    with (
        httpx.Client(
            follow_redirects=True,
            event_hooks={"response": [responses.append]},
            limits=httpx.Limits(max_connections=1),
        ) as transport,
        ContreeClient(
            TOKEN,
            base_url=stub_server.base_url,
            project=PROJECT,
            httpx_client=transport,
        ) as client,
    ):
        spec = build_inspect_find_image_by_tag("alpine:latest")
        if operation == "request":
            result = client.request(spec)
            assert result.status == 302
            assert result.headers["location"] == f"{stub.IMAGE_UUID}/"
        elif operation == "stream":
            assert b"".join(client.stream(spec)) == b""
        else:
            assert client.inspect_find_image_by_tag("alpine:latest") == stub.IMAGE_UUID
        assert len(responses) == 1
        assert responses[0].status_code == 302
        assert responses[0].is_closed
        assert responses[0].history == []
        assert len(stub_server.captured) == 1
        assert stub_server.last.path == "/v1/inspect/"


@pytest.mark.parametrize("operation", ["request", "stream", "lookup"])
def test_supplied_async_httpx_client_does_not_follow_redirects(
    stub_server: StubServer, operation: str
) -> None:
    async def scenario() -> None:
        responses: list[httpx.Response] = []

        async def record(response: httpx.Response) -> None:
            responses.append(response)

        async with (
            httpx.AsyncClient(
                follow_redirects=True,
                event_hooks={"response": [record]},
                limits=httpx.Limits(max_connections=1),
            ) as transport,
            ContreeAsyncClient(
                TOKEN,
                base_url=stub_server.base_url,
                project=PROJECT,
                httpx_client=transport,
            ) as client,
        ):
            spec = build_inspect_find_image_by_tag("alpine:latest")
            if operation == "request":
                result = await client.request(spec)
                assert result.status == 302
                assert result.headers["location"] == f"{stub.IMAGE_UUID}/"
            elif operation == "stream":
                assert b"".join([chunk async for chunk in client.stream(spec)]) == b""
            else:
                result = await client.inspect_find_image_by_tag("alpine:latest")
                assert result == stub.IMAGE_UUID
            assert len(responses) == 1
            assert responses[0].status_code == 302
            assert responses[0].is_closed
            assert responses[0].history == []
            assert len(stub_server.captured) == 1
            assert stub_server.last.path == "/v1/inspect/"

    asyncio.run(scenario())
