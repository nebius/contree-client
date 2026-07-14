"""Lazy pagination iterators (iter_images / iter_operations / iter_files)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from tests.stub_server import PAGINATED_IMAGES, StubServer


def image_requests(stub_server: StubServer) -> list[dict[str, list[str]]]:
    return [c.query for c in stub_server.captured if c.path == "/v1/images"]


def test_iter_images_walks_all_pages(
    invoke: Callable[..., Any], stub_server: StubServer
) -> None:
    images = invoke("iter_images", tag="paginated", page_size=2, collect=True)

    assert [str(image.uuid) for image in images] == [
        item["uuid"] for item in PAGINATED_IMAGES
    ]
    # 2 + 2 + 1: the short page ends the iteration
    queries = image_requests(stub_server)
    assert [q.get("offset", ["0"])[0] for q in queries] == ["0", "2", "4"]
    assert {q["limit"][0] for q in queries} == {"2"}


def test_iter_images_total_limit(
    invoke: Callable[..., Any], stub_server: StubServer
) -> None:
    images = invoke("iter_images", tag="paginated", page_size=2, limit=3, collect=True)

    assert len(images) == 3
    # the last page is trimmed to the remaining budget
    queries = image_requests(stub_server)
    assert [q["limit"][0] for q in queries] == ["2", "1"]


def test_iter_images_early_break_stops_fetching(
    invoke: Callable[..., Any], stub_server: StubServer
) -> None:
    images = invoke("iter_images", tag="paginated", page_size=2, take=2)

    assert len(images) == 2
    assert len(image_requests(stub_server)) == 1


def test_iter_images_rejects_bad_page_size(
    invoke: Callable[..., Any], stub_server: StubServer
) -> None:
    with pytest.raises(ValueError, match="page_size"):
        invoke("iter_images", page_size=0, collect=True)
    assert not stub_server.captured


def test_iter_files_and_operations_single_page(
    invoke: Callable[..., Any], stub_server: StubServer
) -> None:
    files = invoke("iter_files", collect=True)
    assert len(files) == 1

    operations = invoke("iter_operations", collect=True)
    assert len(operations) == 1
    assert str(operations[0].status) == "SUCCESS"


def test_iter_images_rejects_page_size_above_server_cap(
    invoke: Callable[..., Any], stub_server: StubServer
) -> None:
    """P2-12: the server caps a page at 1000; a bigger page_size would
    end the iteration on the first (capped) page and lose the tail."""
    with pytest.raises(ValueError, match="between 1 and 1000"):
        invoke("iter_images", page_size=1001, collect=True)
    assert not stub_server.captured
