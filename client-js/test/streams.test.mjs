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
  EVENTS_UNAVAILABLE_OPERATION_UUID,
  EVENTS_UNAVAILABLE_STALLED_OPERATION_UUID,
  IMAGE_UUID,
  OPERATION_UUID,
  RECONNECT_OPERATION_UUID,
  RESET_OPERATION_UUID,
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

test("a connect dropped on a stale socket is retried", async () => {
  // the stub closes the first connection without writing a byte -
  // exactly what a reused keep-alive socket the server already closed
  // looks like; the idempotent connect must retry, not fail. A
  // dedicated stub gives this test a fresh origin: fetch pools
  // per-origin, and a genuinely stale socket left by an earlier test
  // (a real possibility on Windows) would eat the single transparent
  // reconnect this test is about
  const local = await startStub();
  const isolated = new ContreeClient("test-token", { baseUrl: local.baseUrl });
  try {
    const events = [];
    for await (const event of isolated.iterOperationEvents(
      RESET_OPERATION_UUID,
    )) {
      events.push(event);
    }
    assert.deepEqual(
      events.map((event) => event.type),
      ["init", "spawn", "exit"],
    );
  } finally {
    await isolated.close();
    local.stop();
  }
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

test("followOperationEvents retries timeouts until cancellation", async () => {
  let statusChecks = 0;
  const retrying = new ContreeClient("test-token", {
    baseUrl: "http://localhost:1",
    fetch: async () => {
      statusChecks += 1;
      return new Response(
        JSON.stringify({
          uuid: OPERATION_UUID,
          kind: "instance",
          status:
            statusChecks === 1
              ? OperationStatus.EXECUTING
              : OperationStatus.CANCELLED,
          metadata: null,
          result: null,
        }),
        { status: 200 },
      );
    },
  });
  let streamAttempts = 0;
  retrying.iterOperationEvents = async function* () {
    streamAttempts += 1;
    throw new DOMException("stream timed out", "TimeoutError");
  };

  const events = [];
  for await (const event of retrying.followOperationEvents(OPERATION_UUID)) {
    events.push(event);
  }

  assert.deepEqual(events, []);
  assert.equal(streamAttempts, 2);
  assert.equal(statusChecks, 2);
});

test("followOperationEvents does not retry AbortError", async () => {
  const retrying = new ContreeClient("test-token", {
    baseUrl: "http://localhost:1",
  });
  let streamAttempts = 0;
  let statusChecks = 0;
  retrying.iterOperationEvents = async function* () {
    streamAttempts += 1;
    throw new DOMException("request cancelled", "AbortError");
  };
  retrying.getOperationStatus = async () => {
    statusChecks += 1;
    return { status: OperationStatus.EXECUTING };
  };

  await assert.rejects(
    async () => {
      for await (const event of retrying.followOperationEvents(
        OPERATION_UUID,
      )) {
        void event;
      }
    },
    (error) => error.name === "AbortError",
  );
  assert.equal(streamAttempts, 1);
  assert.equal(statusChecks, 0);
});

test("waitOperation drains the stream and fetches the terminal status", async () => {
  const operation = await client.waitOperation(OPERATION_UUID);
  assert.equal(operation.status, OperationStatus.SUCCESS);
});

test("waitOperation falls back to polling when the events route is missing", async () => {
  // /events 404s outright (older backend, proxy that drops the route,
  // ...); the operation itself still finishes
  const operation = await client.waitOperation(
    EVENTS_UNAVAILABLE_OPERATION_UUID,
  );
  assert.equal(operation.status, OperationStatus.SUCCESS);
});

test("followOperationEvents yields a completion event when the events route is missing", async () => {
  // there is no event log to relay, but the caller must still see a
  // terminal completion event, not an iterator that silently ends
  const events = [];
  for await (const event of client.followOperationEvents(
    EVENTS_UNAVAILABLE_OPERATION_UUID,
  )) {
    events.push(event);
  }
  assert.equal(events.length, 1);
  assert.equal(events[0].type, "completion");
  assert.ok(events[0].data instanceof EventDataCompletion);
  assert.equal(events[0].data.status, OperationStatus.SUCCESS);
});

test("the polling fallback still honors the deadline when the operation never finishes", async () => {
  // events unavailable and the operation never finishes: the polling
  // fallback must still honor the deadline instead of spinning forever
  await assert.rejects(
    client.waitOperation(EVENTS_UNAVAILABLE_STALLED_OPERATION_UUID, {
      timeout: 0.3,
    }),
    (error) =>
      error instanceof DOMException &&
      error.name === "TimeoutError" &&
      error.message.includes(EVENTS_UNAVAILABLE_STALLED_OPERATION_UUID),
  );
});
