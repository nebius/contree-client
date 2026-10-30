import assert from "node:assert/strict";
import { test } from "node:test";

import {
  ClosableStreamRepr,
  DirectoryList,
  EventDataStream,
  GrepMatch,
  GrepResult,
  StreamRepr,
} from "../lib/models.js";

function grepRow(path, number, text, type = "match") {
  return {
    path,
    line_number: number,
    absolute_offset: 0,
    line_text: text,
    line_bytes: new TextEncoder().encode(text).length,
    submatches: [],
    type,
  };
}

for (const [name, rows, expected] of [
  ["empty", [], ""],
  ["newline", [grepRow("/a", 2, "hello\n")], "/a:2:hello\n"],
  ["no newline", [grepRow("/a", 2, "last line")], "/a:2:last line\n"],
  ["empty line", [grepRow("/a", 2, "")], "/a:2:\n"],
  ["Unicode", [grepRow("/a b", 2, "привет \t\r\n")], "/a b:2:привет \t\r\n"],
  [
    "multiple files",
    [grepRow("/a", 2, "one\n"), grepRow("/b", 9, "two\n")],
    "/a:2:one\n/b:9:two\n",
  ],
  [
    "context",
    [
      grepRow("/a", 1, "before\n", "context"),
      grepRow("/a", 2, "match\n"),
      grepRow("/a", 3, "after\n", "context"),
    ],
    "/a-1-before\n/a:2:match\n/a-3-after\n",
  ],
  [
    "disjoint context",
    [
      grepRow("/a", 1, "before\n", "context"),
      grepRow("/a", 2, "match\n"),
      grepRow("/a", 6, "next\n"),
      grepRow("/a", 7, "after\n", "context"),
      grepRow("/b", 1, "other\n"),
    ],
    "/a-1-before\n/a:2:match\n--\n/a:6:next\n/a-7-after\n--\n/b:1:other\n",
  ],
  [
    "disjoint matches",
    [grepRow("/a", 1, "one\n"), grepRow("/a", 7, "seven\n")],
    "/a:1:one\n/a:7:seven\n",
  ],
]) {
  test(`grep text: ${name}`, () => {
    const result = GrepResult.fromWire({
      path: "/",
      patterns: ["pattern"],
      matches: rows,
      truncated: true,
    });
    const before = result.toWire();
    assert.equal(result.asText(), expected);
    assert.equal(String(result), expected);
    assert.deepEqual(result.toWire(), before);
    assert.equal(result.truncated, true);
  });
}

for (const type of ["match", "context"]) {
  test(`individual grep row: ${type}`, () => {
    const row = GrepMatch.fromWire(grepRow("/a b", 3, "привет\r\n", type));
    const before = row.toWire();
    const separator = type === "context" ? "-" : ":";
    const expected = `/a b${separator}3${separator}привет\r\n`;
    assert.equal(row.asText(), expected);
    assert.equal(String(row), expected);
    assert.deepEqual(row.toWire(), before);
  });
}

function fileItem(overrides = {}) {
  return {
    size: 12,
    path: "a.txt",
    owner: "alice",
    group: "staff",
    uid: 1000,
    gid: 100,
    mode: 0o100644,
    mtime: 0,
    nlink: 1,
    is_dir: false,
    is_regular: true,
    is_symlink: false,
    is_socket: false,
    is_fifo: false,
    symlink_to: "",
    ...overrides,
  };
}

function directory(files) {
  return DirectoryList.fromWire({ path: "/work", files });
}

test("directory detailed and short text preserve serialization", () => {
  const result = directory([fileItem()]);
  const before = result.toWire();
  const expected = "-rw-r--r-- 1 alice staff 12 1970-01-01 00:00 a.txt\n";
  assert.equal(result.asText(), expected);
  assert.equal(result.asText(false), expected);
  assert.equal(String(result), expected);
  assert.equal(result.asText(true), "a.txt\n");
  assert.deepEqual(result.toWire(), before);
});

test("empty directory has empty text", () => {
  const result = directory([]);
  assert.equal(result.asText(), "");
  assert.equal(result.asText(true), "");
  assert.equal(String(result), "");
});

test("directory preserves extended years in UTC modification times", () => {
  const result = directory([
    fileItem({ mtime: Date.parse("+010000-01-02T03:04:05Z") / 1000 }),
  ]);
  assert.equal(
    result.asText(),
    "-rw-r--r-- 1 alice staff 12 +010000-01-02 03:04 a.txt\n",
  );
});

test("directory aligns metadata and includes symlink targets", () => {
  const result = directory([
    fileItem({
      path: "имя with spaces",
      owner: 7,
      group: "g",
      nlink: 2,
      size: 3,
    }),
    fileItem({
      path: "link",
      owner: "alice",
      group: 100,
      nlink: 12,
      size: 1234,
      mode: 0o120777,
      is_regular: false,
      is_symlink: true,
      symlink_to: "target with spaces",
    }),
  ]);
  assert.equal(
    result.asText(),
    "-rw-r--r--  2 7     g      3 1970-01-01 00:00 имя with spaces\n" +
      "lrwxrwxrwx 12 alice 100 1234 1970-01-01 00:00 link -> target with spaces\n",
  );
  assert.equal(result.asText(true), "имя with spaces\nlink\n");
});

test("directory uses IDs for empty names and counts Unicode code points", () => {
  const result = directory([
    fileItem({ path: "first", owner: "", group: "", uid: 0, gid: 0 }),
    fileItem({ path: "second", owner: "😀", group: "😀" }),
    fileItem({ path: "third", owner: "ab", group: "cd" }),
  ]);
  assert.equal(
    result.asText(),
    "-rw-r--r-- 1 0  0  12 1970-01-01 00:00 first\n" +
      "-rw-r--r-- 1 😀  😀  12 1970-01-01 00:00 second\n" +
      "-rw-r--r-- 1 ab cd 12 1970-01-01 00:00 third\n",
  );
});

for (const [mode, flags, permissions] of [
  [0o755, { is_dir: true }, "drwxr-xr-x"],
  [0o777, { is_symlink: true }, "lrwxrwxrwx"],
  [0o600, { is_socket: true }, "srw-------"],
  [0o600, { is_fifo: true }, "prw-------"],
  [0o644, { is_regular: true }, "-rw-r--r--"],
  [0o104755, {}, "-rwsr-xr-x"],
  [0o102644, {}, "-rw-r-Sr--"],
  [0o041777, {}, "drwxrwxrwt"],
  [0o041666, {}, "drw-rw-rwT"],
  [0o020600, {}, "crw-------"],
  [0o060600, {}, "brw-------"],
]) {
  test(`directory permissions: ${permissions}`, () => {
    const result = directory([fileItem({ mode, ...flags })]);
    assert.ok(result.asText().startsWith(permissions + " "));
  });
}

test("directory keeps server order", () => {
  assert.equal(
    directory([fileItem({ path: "z" }), fileItem({ path: "a" })]).asText(true),
    "z\na\n",
  );
});

for (const Model of [StreamRepr, EventDataStream, ClosableStreamRepr]) {
  for (const [encoding, value, expected] of [
    ["ascii", "hello\n", "hello\n"],
    ["ascii", "", ""],
    ["base64", "aGkK", "hi\n"],
    ["base64", "/w==", "�"],
    ["base64", "a", ""],
  ]) {
    test(`${Model.name} string conversion: ${encoding}/${value}`, () => {
      const stream = new Model({ value, encoding });
      const before = stream.toWire();
      assert.equal(stream.asText(), expected);
      assert.equal(String(stream), expected);
      assert.deepEqual(stream.toWire(), before);
    });
  }
}

for (const close of [undefined, false, true]) {
  test(`closable stream preserves close=${close} and omitted encoding`, () => {
    const stream = new ClosableStreamRepr({ value: "stdin\n", close });
    const before = stream.toWire();
    assert.deepEqual(stream.asBytes(), new TextEncoder().encode("stdin\n"));
    assert.equal(stream.asText(), "stdin\n");
    assert.equal(String(stream), "stdin\n");
    assert.equal(Object.hasOwn(stream.toWire(), "encoding"), false);
    assert.deepEqual(stream.toWire(), before);
  });
}

for (const value of ["", "plain\n", "привет\n"]) {
  test(`closable stream from text: ${value}`, () => {
    const stream = ClosableStreamRepr.fromText(value);
    assert.equal(stream.asText(), value);
    assert.deepEqual(stream.asBytes(), new TextEncoder().encode(value));
    assert.equal(Object.hasOwn(stream.toWire(), "close"), false);
  });
}

test("closable stream from binary bytes", () => {
  const stream = ClosableStreamRepr.fromBytes(new Uint8Array([0, 255]));
  assert.equal(stream.encoding, "base64");
  assert.deepEqual(stream.asBytes(), new Uint8Array([0, 255]));
  assert.equal(String(stream), "\x00�");
});
