import assert from "node:assert/strict";
import { setTimeout as delay } from "node:timers/promises";
import { test } from "node:test";

import { ContreeClient } from "../lib/client.js";
import { APIConnectionError } from "../lib/errors.js";
import { monotonic, RetryPolicy } from "../lib/runtime.js";

function isConnectionTimeout(error) {
  return (
    error instanceof APIConnectionError &&
    error.timedOut === true &&
    error.cause?.name === "TimeoutError"
  );
}

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

function rejectWhenAborted(signal) {
  let watchdog;
  const aborted = new Promise((_, reject) => {
    if (signal.aborted) {
      reject(signal.reason);
      return;
    }
    signal.addEventListener("abort", () => reject(signal.reason), {
      once: true,
    });
  });
  const stalled = new Promise((_, reject) => {
    watchdog = setTimeout(
      () => reject(new Error("client did not abort the request")),
      1000,
    );
  });
  return Promise.race([aborted, stalled]).finally(() => clearTimeout(watchdog));
}

test("retry policy retries an idempotent request timeout", async () => {
  let fetchCalls = 0;
  const client = new ContreeClient("tok", {
    baseUrl: "http://localhost:1",
    timeout: 0.02,
    retry: new RetryPolicy({ delays: [0], maxAttempts: 2 }),
    fetch: (url, options) => {
      fetchCalls += 1;
      if (fetchCalls === 1) {
        return rejectWhenAborted(options.signal);
      }
      return Promise.resolve(new Response("ok", { status: 200 }));
    },
  });

  const response = await client.call({
    method: "GET",
    path: "/x",
    idempotent: true,
  });
  assert.equal(response.status, 200);
  assert.equal(fetchCalls, 2);
});

test("an expired request deadline does not start fetch", async () => {
  let fetchCalls = 0;
  const client = new ContreeClient("tok", {
    baseUrl: "http://localhost:1",
    fetch: async () => {
      fetchCalls += 1;
      return new Response("ok", { status: 200 });
    },
  });

  await assert.rejects(
    client.request({
      method: "GET",
      path: "/x",
      deadline: monotonic() - 1,
    }),
    isConnectionTimeout,
  );
  assert.equal(fetchCalls, 0);
});

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
  await assert.rejects(client.whoami(), isConnectionTimeout);
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

test("a stream deadline bounds a stalled body", async () => {
  const client = new ContreeClient("tok", {
    baseUrl: "http://localhost:1",
    timeout: 0.4,
    fetch: (url, options) =>
      Promise.resolve(
        new Response(stalledBody(options.signal), { status: 200 }),
      ),
  });

  await assert.rejects(
    async () => {
      for await (const chunk of client.stream({
        method: "GET",
        path: "/x",
        deadline: monotonic() + 0.05,
      })) {
        void chunk;
      }
    },
    (error) => error.name === "TimeoutError",
  );
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

test("waitOperation deadline bounds a stalled connection", async () => {
  const client = new ContreeClient("tok", {
    baseUrl: "http://localhost:1",
    timeout: 10,
    fetch: (url, options) => rejectWhenAborted(options.signal),
  });

  await assert.rejects(
    client.waitOperation("00000000-0000-0000-0000-000000000000", {
      timeout: 0.1,
    }),
    (error) => error.name === "TimeoutError",
  );
});

test("waitOperation deadline bounds its terminal status probe", async () => {
  const client = new ContreeClient("tok", {
    baseUrl: "http://localhost:1",
    timeout: 10,
    fetch: (url, options) => rejectWhenAborted(options.signal),
  });
  client.iterOperationEvents = async function* () {
    throw new DOMException("stream timed out", "TimeoutError");
  };

  await assert.rejects(
    client.waitOperation("00000000-0000-0000-0000-000000000000", {
      timeout: 0.1,
    }),
    (error) => error.name === "TimeoutError",
  );
});

test("waitOperation deadline bounds its final status fetch", async () => {
  const client = new ContreeClient("tok", {
    baseUrl: "http://localhost:1",
    timeout: 10,
    fetch: (url, options) => rejectWhenAborted(options.signal),
  });
  client.followOperationEvents = async function* () {};

  await assert.rejects(
    client.waitOperation("00000000-0000-0000-0000-000000000000", {
      timeout: 0.1,
    }),
    (error) => error.name === "TimeoutError",
  );
});
