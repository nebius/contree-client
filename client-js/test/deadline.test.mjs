import assert from "node:assert/strict";
import { setTimeout as delay } from "node:timers/promises";
import { test } from "node:test";

import { ContreeClient } from "../lib/client.js";
import { monotonic } from "../lib/runtime.js";

/** A body stream that stays silent forever but honours the abort
 * signal, exactly like a real fetch body does. */
function stalledBody(signal) {
  return new ReadableStream({
    start(controller) {
      signal?.addEventListener("abort", () => {
        try {
          controller.error(signal.reason);
        } catch {
          // already closed
        }
      });
    },
  });
}

test("a non-2xx response with an endless body times out", async () => {
  const client = new ContreeClient("tok", {
    baseUrl: "http://localhost:1",
    timeout: 0.2,
    fetch: (url, options) =>
      Promise.resolve(
        new Response(stalledBody(options.signal), { status: 500 }),
      ),
  });
  const started = monotonic();
  await assert.rejects(
    client.whoami(),
    (error) => error.name === "TimeoutError",
  );
  assert.ok(monotonic() - started < 2);
});

test("the stream timer never covers consumer processing time", async () => {
  const client = new ContreeClient("tok", {
    baseUrl: "http://localhost:1",
    timeout: 0.1,
    fetch: (url, options) => {
      const body = new ReadableStream({
        async start(controller) {
          options.signal?.addEventListener("abort", () => {
            try {
              controller.error(options.signal.reason);
            } catch {
              // already closed
            }
          });
          controller.enqueue(new Uint8Array([1]));
          await delay(50);
          controller.enqueue(new Uint8Array([2]));
          controller.close();
        },
      });
      return Promise.resolve(new Response(body, { status: 200 }));
    },
  });
  const chunks = [];
  for await (const chunk of client.stream({ method: "GET", path: "/x" })) {
    chunks.push(chunk);
    // a consumer far slower than the transport timeout must not be
    // aborted while it processes the chunk
    await delay(300);
  }
  assert.equal(chunks.length, 2);
});

test("waitOperation deadline bounds an idle SSE stream", async () => {
  const client = new ContreeClient("tok", {
    baseUrl: "http://localhost:1",
    fetch: (url, options) =>
      Promise.resolve(
        new Response(stalledBody(options.signal), {
          status: 200,
          headers: { "Content-Type": "text/event-stream" },
        }),
      ),
  });
  const started = monotonic();
  await assert.rejects(
    client.waitOperation("00000000-0000-0000-0000-000000000000", {
      timeout: 0.3,
    }),
    (error) => error.name === "TimeoutError",
  );
  assert.ok(monotonic() - started < 2);
});
