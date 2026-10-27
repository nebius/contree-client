import assert from "node:assert/strict";
import { test } from "node:test";
import { ContreeClient } from "../lib/client.js";

const operationId = "12345678-9abc-baba-deda-0123456789ab";
const encoder = new TextEncoder();
function frame(id, type = "completion") {
  return encoder.encode(
    `id: ${id}\ndata: ${JSON.stringify({
      id,
      type,
      ts: "2026-01-01T00:00:00Z",
      spid: 0,
      data:
        type === "completion"
          ? { status: "SUCCESS", duration_ms: 1 }
          : { value: "x", encoding: "ascii" },
    })}\n\n`,
  );
}

function enableTimers(t) {
  // Node 18 uses the original array-based MockTimers API.
  const timers = ["setTimeout"];
  t.mock.timers.enable(
    Number(process.versions.node.split(".")[0]) < 20
      ? timers
      : { apis: timers },
  );
}

function deferred() {
  let resolve;
  const promise = new Promise((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

for (const outcome of ["SUCCESS", "FAILED", "CANCELLED", null]) {
  test(
    outcome === null
      ? "delayed status keeps the original deadline"
      : `completion waits for ${outcome} status`,
    async (t) => {
      enableTimers(t);
      let clock = 100_000;
      t.mock.method(performance, "now", () => clock);
      const polled = deferred();
      const reads = [];
      const client = new ContreeClient("test", {
        fetch: async (url) => {
          if (url.includes("/events")) return new Response(frame(0));
          const status = reads.length ? outcome : "EXECUTING";
          reads.push(status);
          return new Response(JSON.stringify({ status }));
        },
      });
      const sleep = globalThis.setTimeout;
      t.mock.method(globalThis, "setTimeout", (fn, ms, ...args) => {
        const timer = sleep(fn, ms, ...args);
        if (ms === 500 && reads.length) polled.resolve();
        return timer;
      });
      const result = client.waitOperation(operationId, {
        followEvents: true,
        timeout: 10,
      });
      await polled.promise;
      if (outcome === null) {
        const failure = assert.rejects(result, { name: "TimeoutError" });
        clock += 10_000;
        t.mock.timers.tick(10_000);
        await failure;
        assert.deepEqual(reads, ["EXECUTING"]);
      } else {
        clock += 500;
        t.mock.timers.tick(500);
        assert.equal((await result).status, outcome);
        assert.deepEqual(reads, ["EXECUTING", outcome]);
      }
    },
  );
}

test("event progress postpones status probes; keepalives do not", async (t) => {
  enableTimers(t);
  let clock = 100_000;
  t.mock.method(performance, "now", () => clock);
  let requested = deferred();
  let controller;
  let active = false;
  const calls = [];
  const client = new ContreeClient("test", {
    fetch: async (url, options) => {
      assert.equal(
        active,
        false,
        "a status request must not wait behind the SSE connection",
      );
      if (!url.includes("/events")) {
        calls.push("status");
        return new Response(JSON.stringify({ status: "SUCCESS" }));
      }
      calls.push("events");
      active = true;
      const body = new ReadableStream(
        {
          start(value) {
            controller = value;
            options.signal.addEventListener(
              "abort",
              () => {
                active = false;
                // Fetch can report AbortError even when the signal reason is TimeoutError.
                controller.error(new DOMException("aborted", "AbortError"));
              },
              { once: true },
            );
          },
          pull() {
            requested.resolve();
          },
          cancel() {
            active = false;
          },
        },
        { highWaterMark: 0 },
      );
      return new Response(body);
    },
  });
  const result = client.waitOperation(operationId, {
    followEvents: true,
    timeout: 10,
  });
  await requested.promise;
  for (let id = 0; id < 4; id++) {
    clock += 400;
    t.mock.timers.tick(400);
    requested = deferred();
    controller.enqueue(frame(id, "stdout"));
    await requested.promise;
    assert.deepEqual(calls, ["events"]);
  }
  clock += 400;
  t.mock.timers.tick(400);
  requested = deferred();
  controller.enqueue(encoder.encode(": keepalive\n\n"));
  await requested.promise;
  assert.deepEqual(calls, ["events"]);
  clock += 100;
  t.mock.timers.tick(100);
  assert.equal((await result).status, "SUCCESS");
  assert.equal(active, false);
  assert.deepEqual(calls, ["events", "status", "status"]);
});
