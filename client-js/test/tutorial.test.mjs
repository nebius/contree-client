import assert from "node:assert/strict";
import { createWriteStream, readFileSync } from "node:fs";
import { mkdtemp, readFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { pipeline } from "node:stream/promises";
import { test } from "node:test";

import { ContreeClient } from "../lib/testing.js";

const tutorial = readFileSync(
  new URL("../../docs/tutorial.md", import.meta.url),
  "utf8",
);
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
const uuid = "12345678-9abc-baba-deda-0123456789ab";
const hosts = Buffer.from("127.0.0.1 localhost\n");
const tar = Buffer.alloc(1024); // Two zero blocks form an empty tar archive.

function snippet(name) {
  const marker = `<!-- name: ${name};`;
  const start = tutorial.indexOf(marker);
  assert.notEqual(start, -1, `missing ${name} tutorial example`);
  const fence = tutorial.indexOf("```js\n", start) + 6;
  const end = tutorial.indexOf("\n```", fence);
  assert.ok(end > fence, `invalid ${name} code fence`);
  // Inject the standard Node imports so each example can write into its own
  // temporary directory without changing the process working directory.
  return tutorial.slice(fence, end).replace(/^import .*;\n/gm, "");
}

async function runSnippet(name, fixtures) {
  await new AsyncFunction(...Object.keys(fixtures), snippet(name))(
    ...Object.values(fixtures),
  );
}

function subprocessClient(events, createError = null) {
  const client = new ContreeClient();
  client.mock("spawnInstance", { uuid: "operation-1" });
  client.mock("followOperationEvents", events);
  client.mock("operationSubprocessCreate", 2, { error: createError });
  client.mock("cancelOperation");
  return client;
}

function subprocessEvents() {
  return [
    { id: 0, type: "init", spid: 0, data: {} },
    { id: 1, type: "spawn", spid: 1, data: { command: "sleep 60" } },
    { id: 2, type: "spawn", spid: 2, data: { command: "id" } },
    {
      id: 3,
      type: "stdout",
      spid: 2,
      data: { value: "uid=0(root)\n", encoding: "ascii" },
    },
    { id: 4, type: "exit", spid: 9, data: { code: 0 } },
    { id: 5, type: "exit", spid: 2, data: { code: 0 } },
    { id: 6, type: "exit", spid: 1, data: { signal: 15 } },
    { id: 7, type: "completion", spid: 0, data: { status: "CANCELLED" } },
  ];
}

test("tutorial subprocesses start after readiness and wait for operation completion", async () => {
  const frames = subprocessEvents();
  let closed = false;
  const client = subprocessClient(
    (async function* () {
      try {
        for (const event of frames) {
          if (event.id === 1)
            assert.equal(
              client.callsFor("operationSubprocessCreate").length,
              0,
            );
          if (event.id === 2)
            assert.equal(
              client.callsFor("operationSubprocessCreate").length,
              1,
            );
          if (event.id === 5)
            assert.equal(client.callsFor("cancelOperation").length, 0);
          if (event.id === 6)
            assert.equal(client.callsFor("cancelOperation").length, 1);
          yield event;
        }
      } finally {
        closed = true;
      }
    })(),
  );
  const output = [];
  await runSnippet("tutorial_subprocesses", {
    client,
    console: { log: (...args) => output.push(args) },
  });
  assert.deepEqual(
    client.calls.map((call) => call.operation),
    [
      "spawnInstance",
      "followOperationEvents",
      "operationSubprocessCreate",
      "cancelOperation",
    ],
  );
  assert.deepEqual(client.callsFor("spawnInstance")[0].args, [
    "sleep 60",
    "tag:ubuntu:latest",
    { shell: true, timeout: 60, disposable: true },
  ]);
  assert.deepEqual(client.callsFor("operationSubprocessCreate")[0].args, [
    "operation-1",
    "id",
  ]);
  assert.deepEqual(
    output.filter(([label]) => label === "event:").map(([, event]) => event),
    frames,
  );
  assert.deepEqual(
    output.filter(([label]) => label !== "event:").map(([label]) => label),
    ["started subprocess:", "subprocess exited:", "operation completed:"],
  );
  assert.ok(closed);
});

test("tutorial subprocesses reject missing exit or completion events", async () => {
  const frames = subprocessEvents();
  for (const events of [
    [frames[0], frames[7]],
    [...frames.slice(0, 5), frames[7]],
    frames.slice(0, 7),
  ]) {
    await assert.rejects(
      runSnippet("tutorial_subprocesses", {
        client: subprocessClient(events),
        console: { log() {} },
      }),
      /before subprocess exit and operation completion/,
    );
  }
});

test("tutorial subprocesses close the event stream when creation fails", async () => {
  const failure = new Error("subprocess creation failed");
  let closed = false;
  const events = (async function* () {
    try {
      yield* subprocessEvents();
    } finally {
      closed = true;
    }
  })();
  const client = subprocessClient(events, failure);
  await assert.rejects(
    runSnippet("tutorial_subprocesses", { client, console: { log() {} } }),
    (error) => error === failure,
  );
  assert.ok(closed);
  assert.equal(client.callsFor("cancelOperation").length, 0);
});

test("tutorial grep passes patterns, roots, limits, and context", async () => {
  const client = new ContreeClient();
  const output = [];
  client.mock("inspectImageGrep", {
    matches: [
      {
        path: "/etc/hosts",
        lineNumber: 1,
        lineText: hosts.toString(),
        type: "match",
      },
    ],
    truncated: true,
  });
  await runSnippet("tutorial_grep", {
    client,
    uuid,
    console: { log: (...args) => output.push(args.join(" ")) },
  });
  const [call] = client.callsFor("inspectImageGrep");
  assert.deepEqual(call.args, [
    uuid,
    ["^root:", "localhost"],
    {
      path: ["/etc/hosts", "/etc/passwd"],
      glob: ["!*.bak"],
      case: "sensitive",
      before: 1,
      after: 1,
      maxTotal: 20,
    },
  ]);
  assert.match(output[0], /\/etc\/hosts:1 \[match\] 127\.0\.0\.1 localhost/);
  assert.match(output[1], /Search stopped early/);
});

function downloadClient(error = null) {
  const client = new ContreeClient();
  client.mock("inspectImage", { uuid });
  client.mock("inspectImageList", { path: "/etc", files: [] });
  client.mock("inspectImageDownload", hosts);
  client.mock(
    "inspectImageDownloadStream",
    [hosts.subarray(0, 5), hosts.subarray(5)],
    {
      error,
    },
  );
  client.mock("checkImageFile", true);
  client.mock("checkImageArchive", true);
  client.mock("inspectImageArchive", [tar]);
  return client;
}

test("tutorial downloads save exact file and tar bytes and close outputs", async (t) => {
  const directory = await mkdtemp(join(tmpdir(), "contree-tutorial-"));
  t.after(() => rm(directory, { recursive: true, force: true }));
  const client = downloadClient();
  const outputs = [];
  await runSnippet("tutorial_downloads", {
    client,
    uuid,
    console: { log() {} },
    pipeline,
    createWriteStream(name) {
      const stream = createWriteStream(join(directory, name));
      outputs.push(stream);
      return stream;
    },
  });
  assert.deepEqual(await readFile(join(directory, "hosts")), hosts);
  assert.deepEqual(await readFile(join(directory, "etc.tar")), tar);
  assert.ok(outputs.every((stream) => stream.closed));
  assert.deepEqual(client.callsFor("inspectImageArchive")[0].args, [
    uuid,
    "/etc",
  ]);
});

test("tutorial pipeline propagates interrupted downloads and closes output", async (t) => {
  const directory = await mkdtemp(join(tmpdir(), "contree-tutorial-error-"));
  t.after(() => rm(directory, { recursive: true, force: true }));
  const failure = new Error("download interrupted");
  const client = downloadClient(failure);
  let output;
  await assert.rejects(
    runSnippet("tutorial_downloads", {
      client,
      uuid,
      console: { log() {} },
      pipeline,
      createWriteStream(name) {
        output = createWriteStream(join(directory, name));
        return output;
      },
    }),
    (error) => error === failure,
  );
  assert.ok(output.destroyed);
  assert.equal(client.callsFor("inspectImageArchive").length, 0);
});
