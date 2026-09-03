import assert from "node:assert/strict";
import { test } from "node:test";

import { ContreeError, NotFoundError } from "../lib/errors.js";
import { InstanceSpawnResponse, OperationEvent } from "../lib/models.js";
// the documented specifier: package.json exports "./testing" and the
// self-reference resolves it even inside the package's own tests
import { ContreeClient } from "contree-client/testing";
import { OPERATION_UUID } from "./stub.mjs";

test("the double replays mocked results and records calls", async () => {
  const client = new ContreeClient();
  client.mock(
    "spawnInstance",
    InstanceSpawnResponse.fromWire({ uuid: OPERATION_UUID }),
  );
  const response = await client.spawnInstance("echo hi", "tag:busybox:latest", {
    shell: true,
  });
  assert.equal(response.uuid, OPERATION_UUID);
  const calls = client.callsFor("spawnInstance");
  assert.equal(calls.length, 1);
  assert.equal(calls[0].args[0], "echo hi");
});

test("a mock queue steps through outcomes, the last one is sticky", async () => {
  const client = new ContreeClient();
  client.mock("whoami", { step: 1 });
  client.mock("whoami", { step: 2 });
  assert.equal((await client.whoami()).step, 1);
  assert.equal((await client.whoami()).step, 2);
  assert.equal((await client.whoami()).step, 2);
});

test("mocked errors and iterator operations", async () => {
  const client = new ContreeClient();
  client.mock("getFile", null, { error: new NotFoundError(404, "nope") });
  await assert.rejects(client.getFile("a".repeat(64)), NotFoundError);

  const events = [
    OperationEvent.fromWire({
      id: 0,
      ts: "2026-06-08T20:00:00Z",
      spid: 0,
      type: "init",
      data: {},
    }),
  ];
  client.mock("iterOperationEvents", events);
  const seen = [];
  for await (const event of client.iterOperationEvents(OPERATION_UUID)) {
    seen.push(event);
  }
  assert.deepEqual(seen, events);
});

test("unmocked operations fail loudly, unknown names are rejected", async () => {
  const client = new ContreeClient();
  await assert.rejects(
    client.whoami(),
    (error) =>
      !(error instanceof ContreeError) &&
      /no mock configured/.test(error.message),
  );
  assert.throws(() => client.mock("nonexistentMethod"), TypeError);
});

test("constructedWith records the construction arguments", () => {
  const client = new ContreeClient("tok", {
    baseUrl: "http://localhost:1",
    project: "proj",
    identity: "my-cli/1.2.3",
  });
  assert.equal(client.constructedWith.token, "tok");
  assert.equal(client.constructedWith.project, "proj");
  assert.equal(client.constructedWith.identity, "my-cli/1.2.3");
});
