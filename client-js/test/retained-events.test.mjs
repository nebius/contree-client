import assert from "node:assert/strict";
import { createServer } from "node:http";
import { once } from "node:events";
import { createGzip, constants } from "node:zlib";
import { test } from "node:test";
import { ContreeClient } from "../lib/client.js";
import { APIConnectionError, APIStatusError } from "../lib/errors.js";
import { RetryPolicy } from "../lib/runtime.js";

const operationId = "87654321-9abc-baba-deda-0123456789ab";
const encoder = new TextEncoder();
function frame(id, type = "stdout") {
  return `id: ${id}\ndata: ${JSON.stringify({
    id,
    type,
    spid: 7,
    ts: "2026-10-28T00:00:00Z",
    data:
      type === "completion"
        ? { status: "SUCCESS", duration_ms: 1 }
        : { value: "tail", encoding: "ascii" },
  })}\n\n`;
}
async function collect(source) {
  const events = [];
  for await (const event of source) events.push(event.id);
  return events;
}
function deferred() {
  let resolve;
  const promise = new Promise((done) => {
    resolve = done;
  });
  return { promise, resolve };
}
function fakeClock(t) {
  let now = 100_000;
  const timers = ["setTimeout"];
  t.mock.timers.enable(
    Number(process.versions.node.split(".")[0]) < 20
      ? timers
      : { apis: timers },
  );
  t.mock.method(performance, "now", () => now);
  return (ms) => {
    now += ms;
    t.mock.timers.tick(ms);
  };
}

for (const status of ["SUCCESS", "FAILED", "CANCELLED"]) {
  for (const ending of ["eof", "sse_error", "reset"]) {
    test(
      `native gzip SSE drains retained tail after ${ending} and ${status}`,
      { timeout: 10_000 },
      async (t) => {
        const firstClosed = deferred();
        const resetLive = deferred();
        const tailClosed = deferred();
        const calls = [];
        const failures = [];
        const server = createServer((request, response) => {
          void (async () => {
            const url = new URL(request.url, "http://localhost");
            if (!url.pathname.endsWith("/events")) {
              calls.push("status");
              await firstClosed.promise;
              response.end(JSON.stringify({ status }));
              return;
            }
            const live = url.searchParams.get("follow") === "1";
            calls.push(live ? "live" : "retained");
            assert.equal(url.searchParams.get("spid"), "7");
            assert.equal(url.searchParams.get("since"), "9");
            if (!live) {
              await firstClosed.promise;
              assert.equal(request.headers["last-event-id"], "10");
            }
            response.on("close", () =>
              (live ? firstClosed : tailClosed).resolve(),
            );
            response.writeHead(200, {
              "Content-Type": "text/event-stream",
              "Content-Encoding": "gzip",
            });
            const gzip = createGzip();
            gzip.pipe(response);
            response.on("close", () => gzip.destroy());
            if (live && ending === "eof") gzip.end(frame(10));
            else {
              gzip.write(
                live
                  ? frame(10) +
                      (ending === "reset"
                        ? ""
                        : "event: sse_error\ndata: interrupted\n\n")
                  : frame(11) + frame(12, "completion"),
              );
              gzip.flush(constants.Z_SYNC_FLUSH);
              if (live && ending === "reset") {
                await resetLive.promise;
                response.destroy();
              }
              // Keep the response open: the follower must cancel it itself.
            }
          })().catch((error) => {
            failures.push(error);
            response.destroy();
          });
        });
        server.listen(0, "127.0.0.1");
        await once(server, "listening");
        t.after(async () => {
          server.closeAllConnections();
          await new Promise((resolve) => server.close(resolve));
          assert.deepEqual(failures, []);
        });
        const client = new ContreeClient("test", {
          baseUrl: `http://127.0.0.1:${server.address().port}`,
        });
        const events = [];
        for await (const event of client.followOperationEvents(operationId, {
          spid: 7,
          since: 9,
          timeout: 5,
        })) {
          events.push(event.id);
          if (event.id === 10) resetLive.resolve();
        }
        assert.deepEqual(events, [10, 11, 12]);
        await tailClosed.promise;
        assert.deepEqual(calls, ["live", "status", "retained"]);
      },
    );
  }
}

for (const options of [{}, { spid: 0 }, { since: 0 }, { lastEventId: 0 }]) {
  test(`retained clean EOF contract: ${JSON.stringify(options)}`, async () => {
    const calls = [];
    const client = new ContreeClient("test", {
      fetch: async (url, init) => {
        const query = new URL(url).searchParams;
        if (!url.includes("/events"))
          return new Response('{"status":"SUCCESS"}');
        calls.push({
          follow: query.get("follow") ?? "0",
          cursor: new Headers(init.headers).get("last-event-id"),
        });
        return new Response(frame(calls.length === 1 ? 10 : 11));
      },
    });
    const events = [];
    const read = async () => {
      for await (const event of client.followOperationEvents(
        operationId,
        options,
      ))
        events.push(event.id);
    };
    if (Object.keys(options).length) await read();
    else
      await assert.rejects(
        read(),
        (error) =>
          error instanceof APIConnectionError &&
          /without completion/.test(error.message),
      );
    assert.deepEqual(events, [10, 11]);
    assert.deepEqual(
      calls.map((call) => call.follow),
      ["1", "0"],
    );
    assert.equal(calls[1].cursor, "10");
  });
}

for (const failure of ["network", "sse_error", 425, 502]) {
  test(`retained recovery has three default attempts: ${failure}`, async () => {
    let reads = 0;
    let probes = 0;
    const client = new ContreeClient("test", {
      fetch: async (url) => {
        if (!url.includes("/events")) {
          probes++;
          return new Response('{"status":"SUCCESS"}');
        }
        if (++reads === 1) return new Response(frame(10));
        if (failure === "network") throw new TypeError("connection failed");
        if (failure === "sse_error")
          return new Response("event: sse_error\ndata: failed\n\n");
        return new Response("unavailable", { status: failure });
      },
    });
    await assert.rejects(
      collect(client.followOperationEvents(operationId)),
      (error) => error instanceof APIConnectionError && error.cause != null,
    );
    assert.equal(reads, 4);
    assert.equal(probes, 1);
  });
}

for (const [status, policy, attempts] of [
  [404, undefined, 1],
  [500, new RetryPolicy({ serverErrors: false, delays: [0] }), 1],
  [418, new RetryPolicy({ statuses: [418], delays: [0], maxAttempts: 2 }), 2],
]) {
  test(`retained HTTP ${status} honors retry policy`, async () => {
    let reads = 0;
    const client = new ContreeClient("test", {
      retry: policy,
      fetch: async (url) => {
        if (!url.includes("/events"))
          return new Response('{"status":"SUCCESS"}');
        return ++reads === 1
          ? new Response(frame(10))
          : new Response("failed", { status });
      },
    });
    await assert.rejects(
      collect(client.followOperationEvents(operationId)),
      (error) =>
        error instanceof APIConnectionError &&
        error.cause instanceof APIStatusError &&
        error.cause.status === status,
    );
    assert.equal(reads, 1 + attempts);
  });
}

test("an interrupted retained read resumes from its latest cursor", async () => {
  const calls = [];
  const client = new ContreeClient("test", {
    retry: new RetryPolicy({ delays: [0] }),
    fetch: async (url, init) => {
      if (!url.includes("/events")) return new Response('{"status":"SUCCESS"}');
      calls.push({
        follow: new URL(url).searchParams.get("follow") ?? "0",
        cursor: new Headers(init.headers).get("last-event-id"),
      });
      return new Response(
        calls.length === 1
          ? frame(10)
          : calls.length === 2
            ? frame(11) + "id: 12\nevent: sse_error\ndata: interrupted\n\n"
            : frame(13, "completion"),
      );
    },
  });
  assert.deepEqual(
    await collect(client.followOperationEvents(operationId)),
    [10, 11, 13],
  );
  assert.deepEqual(calls, [
    { follow: "1", cursor: null },
    { follow: "0", cursor: "10" },
    { follow: "0", cursor: "12" },
  ]);
});

for (const ending of ["complete", "incomplete"]) {
  test(`waitOperation verifies retained log: ${ending}`, async () => {
    const calls = [];
    const client = new ContreeClient("test", {
      fetch: async (url) => {
        if (!url.includes("/events")) {
          calls.push("status");
          return new Response('{"status":"SUCCESS"}');
        }
        const live = new URL(url).searchParams.get("follow") === "1";
        calls.push(live ? "live" : "retained");
        return new Response(
          live
            ? frame(10)
            : frame(11) +
                (ending === "complete" ? frame(12, "completion") : ""),
        );
      },
    });
    const result = client.waitOperation(operationId, { followEvents: true });
    if (ending === "complete") assert.equal((await result).status, "SUCCESS");
    else await assert.rejects(result, APIConnectionError);
    assert.deepEqual(
      calls,
      ending === "complete"
        ? ["live", "status", "retained", "status"]
        : ["live", "status", "retained"],
    );
  });
}

for (const failure of [
  new SyntaxError("bad JSON"),
  new DOMException("cancelled", "AbortError"),
]) {
  test(`retained ${failure.name} propagates without retry`, async () => {
    let reads = 0;
    const client = new ContreeClient("test", {
      fetch: async (url) => {
        if (!url.includes("/events"))
          return new Response('{"status":"SUCCESS"}');
        if (++reads === 1) return new Response(frame(10));
        if (failure instanceof SyntaxError)
          return new Response("data: {broken\n\n");
        return new Response(
          new ReadableStream({
            start(controller) {
              controller.error(failure);
            },
          }),
        );
      },
    });
    await assert.rejects(
      collect(client.followOperationEvents(operationId)),
      (error) =>
        failure instanceof SyntaxError
          ? error instanceof SyntaxError
          : error === failure,
    );
    assert.equal(reads, 2);
  });
}

for (const ending of ["idle", "deadline"]) {
  test(`retained stream keeps the original ${ending} budget`, async (t) => {
    const advance = fakeClock(t);
    let requested = deferred();
    let controller;
    let closed = false;
    let reads = 0;
    let probes = 0;
    const client = new ContreeClient("test", {
      retry: new RetryPolicy({ delays: [0], maxAttempts: 1 }),
      fetch: async (url, init) => {
        if (!url.includes("/events")) {
          probes++;
          advance(4000);
          return new Response('{"status":"SUCCESS"}');
        }
        if (++reads === 1) return new Response(frame(10));
        return new Response(
          new ReadableStream(
            {
              start(value) {
                controller = value;
                init.signal.addEventListener(
                  "abort",
                  () => {
                    closed = true;
                    controller.error(new DOMException("aborted", "AbortError"));
                  },
                  { once: true },
                );
              },
              pull() {
                requested.resolve();
              },
              cancel() {
                closed = true;
              },
            },
            { highWaterMark: 0 },
          ),
        );
      },
    });
    const result = collect(
      client.followOperationEvents(operationId, {
        timeout: 10,
        idleTimeout: 3,
      }),
    );
    const failure = assert.rejects(result, (error) =>
      ending === "deadline"
        ? error.name === "TimeoutError"
        : error instanceof APIConnectionError &&
          error.cause instanceof APIConnectionError &&
          error.cause.timedOut,
    );
    await requested.promise;
    for (let i = 0; i < 2; i++) {
      advance(2500);
      requested = deferred();
      controller.enqueue(encoder.encode(": ping\n\n"));
      await requested.promise;
      if (ending === "idle") break;
    }
    advance(ending === "deadline" ? 1000 : 3000);
    await failure;
    assert.equal(closed, true);
    assert.equal(reads, 2);
    assert.equal(probes, 1);
  });
}

test("retained retry delays do not extend the overall deadline", async (t) => {
  const advance = fakeClock(t);
  const sleeping = deferred();
  let reads = 0;
  const sleep = globalThis.setTimeout;
  t.mock.method(globalThis, "setTimeout", (fn, ms, ...args) => {
    const timer = sleep(fn, ms, ...args);
    if (reads === 2 && ms === 1000) sleeping.resolve();
    return timer;
  });
  const client = new ContreeClient("test", {
    retry: new RetryPolicy({ delays: [20], maxAttempts: 3 }),
    fetch: async (url) => {
      if (!url.includes("/events")) return new Response('{"status":"SUCCESS"}');
      return ++reads === 1
        ? new Response(frame(10))
        : new Response("retry", { status: 503 });
    },
  });
  const failure = assert.rejects(
    collect(client.followOperationEvents(operationId, { timeout: 1 })),
    { name: "TimeoutError" },
  );
  await sleeping.promise;
  advance(1000);
  await failure;
  assert.equal(reads, 2);
});
