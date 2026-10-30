from __future__ import annotations

import stat
from types import ModuleType
from typing import Any

import pytest


def grep_row(path: str, number: int, text: str, kind: str = "match") -> dict[str, Any]:
    return {
        "path": path,
        "line_number": number,
        "absolute_offset": 0,
        "line_text": text,
        "line_bytes": len(text.encode()),
        "submatches": [],
        "type": kind,
    }


@pytest.mark.parametrize(
    ("rows", "expected"),
    [
        ([], ""),
        ([grep_row("/a", 2, "hello\n")], "/a:2:hello\n"),
        ([grep_row("/a", 2, "last line")], "/a:2:last line\n"),
        ([grep_row("/a", 2, "")], "/a:2:\n"),
        ([grep_row("/a b", 2, "привет \t\r\n")], "/a b:2:привет \t\r\n"),
        (
            [grep_row("/a", 2, "one\n"), grep_row("/b", 9, "two\n")],
            "/a:2:one\n/b:9:two\n",
        ),
        (
            [
                grep_row("/a", 1, "before\n", "context"),
                grep_row("/a", 2, "match\n"),
                grep_row("/a", 3, "after\n", "context"),
            ],
            "/a-1-before\n/a:2:match\n/a-3-after\n",
        ),
        (
            [
                grep_row("/a", 1, "before\n", "context"),
                grep_row("/a", 2, "match\n"),
                grep_row("/a", 6, "next\n"),
                grep_row("/a", 7, "after\n", "context"),
                grep_row("/b", 1, "other\n"),
            ],
            "/a-1-before\n/a:2:match\n--\n/a:6:next\n/a-7-after\n--\n/b:1:other\n",
        ),
        (
            [grep_row("/a", 1, "one\n"), grep_row("/a", 7, "seven\n")],
            "/a:1:one\n/a:7:seven\n",
        ),
    ],
)
def test_grep_text(
    models: ModuleType, rows: list[dict[str, Any]], expected: str
) -> None:
    payload = {"path": "/", "patterns": ["pattern"], "matches": rows, "truncated": True}
    result = models.GrepResult.from_dict(payload)
    before = result.to_dict()
    assert result.as_text() == expected
    assert str(result) == expected
    assert result.to_dict() == before
    assert result.truncated is True


def file_item(**overrides: Any) -> dict[str, Any]:
    return {
        "size": 12,
        "path": "a.txt",
        "owner": "alice",
        "group": "staff",
        "uid": 1000,
        "gid": 100,
        "mode": stat.S_IFREG | 0o644,
        "mtime": 0,
        "nlink": 1,
        "is_dir": False,
        "is_regular": True,
        "is_symlink": False,
        "is_socket": False,
        "is_fifo": False,
        "symlink_to": "",
        **overrides,
    }


def test_directory_long_and_short_text(models: ModuleType) -> None:
    result = models.DirectoryList.from_dict({"path": "/work", "files": [file_item()]})
    before = result.to_dict()
    assert result.as_text() == "-rw-r--r-- 1 alice staff 12 1970-01-01 00:00 a.txt\n"
    assert result.as_text(False) == result.as_text()
    assert str(result) == result.as_text()
    assert result.as_text(short=True) == "a.txt\n"
    assert result.as_text(True) == "a.txt\n"
    assert result.to_dict() == before


@pytest.mark.parametrize("short", [False, True])
def test_empty_directory_text(models: ModuleType, short: bool) -> None:
    assert models.DirectoryList(path="/empty", files=[]).as_text(short=short) == ""


def test_directory_alignment_names_and_symlink(models: ModuleType) -> None:
    result = models.DirectoryList.from_dict(
        {
            "path": "/work",
            "files": [
                file_item(path="имя with spaces", owner=7, group="g", nlink=2, size=3),
                file_item(
                    path="link",
                    owner="alice",
                    group=100,
                    nlink=12,
                    size=1234,
                    mode=stat.S_IFLNK | 0o777,
                    is_regular=False,
                    is_symlink=True,
                    symlink_to="target with spaces",
                ),
            ],
        }
    )
    assert result.as_text() == (
        "-rw-r--r--  2 7     g      3 1970-01-01 00:00 имя with spaces\n"
        "lrwxrwxrwx 12 alice 100 1234 1970-01-01 00:00 link -> target with spaces\n"
    )
    assert result.as_text(short=True) == "имя with spaces\nlink\n"


@pytest.mark.parametrize(
    ("mode", "flags", "permissions"),
    [
        (0o755, {"is_dir": True}, "drwxr-xr-x"),
        (0o777, {"is_symlink": True}, "lrwxrwxrwx"),
        (0o600, {"is_socket": True}, "srw-------"),
        (0o600, {"is_fifo": True}, "prw-------"),
        (0o644, {"is_regular": True}, "-rw-r--r--"),
        (stat.S_IFREG | 0o4755, {}, "-rwsr-xr-x"),
        (stat.S_IFREG | 0o2644, {}, "-rw-r-Sr--"),
        (stat.S_IFDIR | 0o1777, {}, "drwxrwxrwt"),
        (stat.S_IFCHR | 0o600, {}, "crw-------"),
        (stat.S_IFBLK | 0o600, {}, "brw-------"),
    ],
)
def test_directory_file_types_and_special_permissions(
    models: ModuleType, mode: int, flags: dict[str, bool], permissions: str
) -> None:
    item = file_item(mode=mode, **flags)
    result = models.DirectoryList.from_dict({"path": "/", "files": [item]})
    assert result.as_text().startswith(permissions + " ")


def test_directory_keeps_server_order(models: ModuleType) -> None:
    result = models.DirectoryList.from_dict(
        {"path": "/", "files": [file_item(path="z"), file_item(path="a")]}
    )
    assert result.as_text(short=True) == "z\na\n"


def test_directory_empty_names_use_numeric_ids(models: ModuleType) -> None:
    result = models.DirectoryList.from_dict(
        {"path": "/", "files": [file_item(owner="", group="", uid=0, gid=42)]}
    )
    assert result.as_text() == "-rw-r--r-- 1 0 42 12 1970-01-01 00:00 a.txt\n"


@pytest.mark.parametrize("kind", ["match", "context"])
@pytest.mark.parametrize("text", ["", "hello", "hello\n", "привет\r\n"])
def test_grep_match_text(models: ModuleType, kind: str, text: str) -> None:
    row = models.GrepMatch.from_dict(grep_row("/a b", 3, text, kind))
    before = row.to_dict()
    separator = "-" if kind == "context" else ":"
    expected = f"/a b{separator}3{separator}{text}"
    if not expected.endswith("\n"):
        expected += "\n"
    assert row.as_text() == expected
    assert str(row) == expected
    assert row.to_dict() == before


@pytest.mark.parametrize(
    "name", ["StreamRepr", "EventDataStream", "ClosableStreamRepr"]
)
@pytest.mark.parametrize(
    ("encoding", "value", "expected"),
    [
        ("ascii", "hello\n", "hello\n"),
        ("ascii", "", ""),
        ("base64", "aGkK", "hi\n"),
        ("base64", "/w==", "\ufffd"),
        ("base64", "a", ""),
    ],
)
def test_stream_text_alias(
    models: ModuleType, name: str, encoding: str, value: str, expected: str
) -> None:
    stream = getattr(models, name)(value=value, encoding=encoding)
    before = stream.to_dict()
    assert stream.as_text() == expected
    assert str(stream) == expected
    assert stream.to_dict() == before


@pytest.mark.parametrize("close", [Ellipsis, False, True])
@pytest.mark.parametrize("value", ["", "stdin\n"])
def test_closable_stream_omitted_encoding(
    models: ModuleType, close: Any, value: str
) -> None:
    stream = models.ClosableStreamRepr(value=value, close=close)
    before = stream.to_dict()
    assert stream.as_bytes() == value.encode()
    assert stream.as_text() == value
    assert str(stream) == value
    assert "encoding" not in stream.to_dict()
    assert stream.to_dict() == before


@pytest.mark.parametrize("value", ["", "plain\n", "привет\n"])
def test_closable_stream_from_text(models: ModuleType, value: str) -> None:
    stream = models.ClosableStreamRepr.from_text(value)
    assert stream.as_text() == value
    assert stream.as_bytes() == value.encode()
    assert stream.encoding == ("ascii" if value.isascii() else "base64")
    assert stream.close is Ellipsis
    assert "close" not in stream.to_dict()


def test_closable_stream_from_bytes(models: ModuleType) -> None:
    stream = models.ClosableStreamRepr.from_bytes(b"\x00\xff")
    assert stream.encoding == "base64"
    assert stream.as_bytes() == b"\x00\xff"
    assert str(stream) == "\x00\ufffd"


@pytest.mark.parametrize(
    "name",
    [
        "GrepMatch",
        "GrepResult",
        "DirectoryList",
        "StreamRepr",
        "EventDataStream",
        "ClosableStreamRepr",
    ],
)
def test_text_method_is_string_alias(models: ModuleType, name: str) -> None:
    model = getattr(models, name)
    assert model.__str__ is model.as_text
