import assert from "node:assert/strict";
import { setTimeout as delay } from "node:timers/promises";
import { test } from "node:test";

import { ContreeClient } from "../lib/client.js";
import { APIConnectionError } from "../lib/errors.js";
import { monotonic } from "../lib/runtime.js";
import { IMAGE_UUID, OPERATION_UUID } from "./stub.mjs";

const statusPath = `/v1/operations/${OPERATION_UUID}`;

function statusResponse(kind, status) {
  return new Response(
    JSON.stringify({
      uuid: OPERATION_UUID,
      kind,
      status,
      error: status === "SUCCESS" ? null : "registry failure or cancellation",
      result_image_uuid: status === "SUCCESS" ? IMAGE_UUID : null,
    }),
    { headers: { "Content-Type": "application/json" } },
  );
}

function completionResponse(status) {
  const event = {
    id: 0,
    ts: "2026-06-08T20:00:00Z",
    spid: 0,
    type: "completion",
    data: { status, error: null, duration_ms: 1 },
  };
  return new Response(`id: 0\ndata: ${JSON.stringify(event)}\n\n`, {
    headers: { "Content-Type": "text/event-stream" },
  });
}

function stall(signal) {
  return new Promise((_, reject) => {
    signal.addEventListener("abort", () => reject(signal.reason), {
      once: true,
    });
  });
}

for (const outcome of ["SUCCESS", "FAILED", "CANCELLED"]) {
  for (const [kind, options] of [
    ["instance", {}],
    ["instance", { followEvents: null }],
    ["instance", { followEvents: true }],
    ["image_import", { followEvents: true }],
  ]) {
    test(`events: ${kind}, ${JSON.stringify(options)}, ${outcome}`, async () => {
      const paths = [];
      const client = new ContreeClient("tok", {
        baseUrl: "http://localhost:1",
        fetch: async (url) => {
          const path = new URL(url).pathname;
          paths.push(path);
          if (path.endsWith("/events")) return completionResponse(outcome);
          return statusResponse(
            kind,
            paths.length === 1 && options.followEvents !== true
              ? "EXECUTING"
              : outcome,
          );
        },
      });
      const result = await client.waitOperation(OPERATION_UUID, options);
      assert.equal(result.status, outcome);
      assert.equal(
        result.error,
        outcome === "SUCCESS" ? null : "registry failure or cancellation",
      );
      const expected = [`${statusPath}/events`, statusPath];
      if (options.followEvents !== true) expected.unshift(statusPath);
      assert.deepEqual(paths, expected);
    });
  }

  for (const [kind, options] of [
    ["image_import", {}],
    ["image_import", { followEvents: null }],
    ["image_import", { followEvents: false }],
    ["instance", { followEvents: false }],
  ]) {
    for (const alreadyTerminal of [false, true]) {
      test(`polling: ${kind}, ${JSON.stringify(options)}, ${outcome}, terminal=${alreadyTerminal}`, async () => {
        const paths = [];
        const client = new ContreeClient("tok", {
          baseUrl: "http://localhost:1",
          fetch: async (url) => {
            paths.push(new URL(url).pathname);
            return statusResponse(
              kind,
              !alreadyTerminal && paths.length === 1 ? "EXECUTING" : outcome,
            );
          },
        });
        const result = await client.waitOperation(OPERATION_UUID, options);
        assert.equal(result.status, outcome);
        assert.equal(
          result.error,
          outcome === "SUCCESS" ? null : "registry failure or cancellation",
        );
        assert.equal(
          result.resultImageUuid,
          outcome === "SUCCESS" ? IMAGE_UUID : null,
        );
        assert.deepEqual(
          paths,
          Array(alreadyTerminal ? 1 : 2).fill(statusPath),
        );
      });
    }
  }
}

for (const followEvents of [null, false]) {
  for (const stalledRead of [0, 1, 2]) {
    test(`polling deadline: followEvents=${followEvents}, stalledRead=${stalledRead}`, async () => {
      const paths = [];
      const client = new ContreeClient("tok", {
        baseUrl: "http://localhost:1",
        fetch: async (url, { signal }) => {
          paths.push(new URL(url).pathname);
          if (paths.length === stalledRead) return stall(signal);
          return statusResponse("image_import", "EXECUTING");
        },
      });
      const started = monotonic();
      await assert.rejects(
        client.waitOperation(OPERATION_UUID, { timeout: 0.65, followEvents }),
        (error) =>
          error.name === "TimeoutError" &&
          error.message.includes(OPERATION_UUID),
      );
      assert.ok(monotonic() - started < 1.5);
      assert.ok(paths.every((path) => path === statusPath));
    });
  }
}

test("kind detection and events share one deadline", async () => {
  const paths = [];
  const client = new ContreeClient("tok", {
    baseUrl: "http://localhost:1",
    fetch: async (url, { signal }) => {
      const path = new URL(url).pathname;
      paths.push(path);
      if (path.endsWith("/events")) return stall(signal);
      await delay(400);
      return statusResponse("instance", "EXECUTING");
    },
  });
  const started = monotonic();
  await assert.rejects(
    client.waitOperation(OPERATION_UUID, { timeout: 0.6 }),
    (error) => error.name === "TimeoutError",
  );
  assert.ok(monotonic() - started < 0.9);
  assert.deepEqual(paths, [statusPath, `${statusPath}/events`]);
});

test("polling preserves connection failures", async () => {
  const client = new ContreeClient("tok", {
    baseUrl: "http://localhost:1",
    fetch: async () => {
      throw new TypeError("disconnected");
    },
  });
  await assert.rejects(
    client.waitOperation(OPERATION_UUID),
    APIConnectionError,
  );
});
