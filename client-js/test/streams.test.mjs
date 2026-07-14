import assert from "node:assert/strict";
import { after, before, test } from "node:test";

import { ContreeClient } from "../lib/client.js";
import {
  EventDataCompletion,
  EventDataExit,
  EventDataSpawn,
  OperationStatus,
} from "../lib/models.js";
import { bytesToText } from "../lib/runtime.js";
import {
  DOWNLOAD_CONTENT,
  IMAGE_UUID,
  OPERATION_UUID,
  RECONNECT_OPERATION_UUID,
  TRUNCATED_IMAGE_UUID,
  startStub,
} from "./stub.mjs";

let stub;
let client;

before(async () => {
  stub = await startStub();
  client = new ContreeClient("test-token", { baseUrl: stub.baseUrl });
});

after(async () => {
  await client.close();
  stub.stop();
});

function concat(chunks) {
  const total = chunks.reduce((size, chunk) => size + chunk.length, 0);
  const merged = new Uint8Array(total);
  let offset = 0;
  for (const chunk of chunks) {
    merged.set(chunk, offset);
    offset += chunk.length;
  }
  return merged;
}

test("buffered and streaming downloads agree", async () => {
  const buffered = await client.inspectImageDownload(IMAGE_UUID, "/etc/hosts");
  assert.equal(bytesToText(buffered), DOWNLOAD_CONTENT);

  const chunks = [];
  for await (const chunk of client.inspectImageDownloadStream(
    IMAGE_UUID,
    "/etc/hosts",
  )) {
    chunks.push(chunk);
  }
  assert.equal(bytesToText(concat(chunks)), DOWNLOAD_CONTENT);
});

test("archives stream as tar bytes", async () => {
  const chunks = [];
  for await (const chunk of client.inspectImageArchive(IMAGE_UUID, "/etc")) {
    chunks.push(chunk);
  }
  const archive = concat(chunks);
  assert.ok(archive.length > 512);
  // a PAX tar starts with the member name; ours contains etc/hosts
  assert.ok(bytesToText(archive.slice(0, 2048)).includes("etc/hosts"));
});

test("a truncated compressed stream ends without hanging", async () => {
  // unlike the Python client (DecompressionError), fetch decodes gzip
  // itself and is lenient about a missing trailer: the truncated tail
  // is silently dropped - assert the stream at least terminates short
  const chunks = [];
  for await (const chunk of client.inspectImageArchive(
    TRUNCATED_IMAGE_UUID,
    "/etc",
  )) {
    chunks.push(chunk);
  }
  const intact = [];
  for await (const chunk of client.inspectImageArchive(IMAGE_UUID, "/etc")) {
    intact.push(chunk);
  }
  assert.ok(concat(chunks).length < concat(intact).length);
});

test("SSE events arrive typed and in order", async () => {
  const events = [];
  for await (const event of client.iterOperationEvents(OPERATION_UUID)) {
    events.push(event);
  }
  assert.deepEqual(
    events.map((event) => event.type),
    ["init", "spawn", "exit"],
  );
  assert.ok(events[1].data instanceof EventDataSpawn);
  assert.equal(events[1].data.pid, 4242);
  assert.ok(events[2].data instanceof EventDataExit);
  assert.ok(events[0].ts instanceof Date);
});

test("followOperationEvents resumes after an in-band stream error", async () => {
  const events = [];
  for await (const event of client.followOperationEvents(
    RECONNECT_OPERATION_UUID,
  )) {
    events.push(event);
  }
  // first connection: init, spawn, then sse_error; the reconnect
  // serves the tail: exit and the authoritative completion
  assert.deepEqual(
    events.map((event) => event.type),
    ["init", "spawn", "exit", "completion"],
  );
  assert.deepEqual(
    events.map((event) => event.id),
    [0, 1, 2, 3],
  );
  assert.ok(events[3].data instanceof EventDataCompletion);
});

test("waitOperation drains the stream and fetches the terminal status", async () => {
  const operation = await client.waitOperation(OPERATION_UUID);
  assert.equal(operation.status, OperationStatus.SUCCESS);
});
