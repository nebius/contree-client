/** Live smoke against a real Contree API.
 *
 * Runs only when the environment describes a profile (CONTREE_TOKEN /
 * CONTREE_URL, see profiles.fromEnv()); performs at most TWO disposable
 * spawns and no imports - the CI token allows 8 concurrent runs and a
 * single concurrent import, and the workflow serializes live runs
 * anyway. Never point these variables at production.
 */

import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import { after, before, test } from "node:test";

import { ContreeClient } from "../../lib/client.js";
import {
  ClosableStreamRepr,
  OperationStatus,
  TERMINAL_STATUSES,
} from "../../lib/models.js";
import { fromEnv } from "../../lib/profiles.js";
import { sleep } from "../../lib/runtime.js";

const profile = fromEnv();
const skip = profile === null && "CONTREE_TOKEN / CONTREE_URL are not set";

let client;
let permissions = {};

before(async () => {
  if (profile === null) {
    return;
  }
  client = new ContreeClient(profile.token, {
    baseUrl: profile.url,
    project: profile.project,
    identity: "contree-client-js-ci/0",
  });
  permissions = (await client.whoami()).permissions ?? {};
});

after(async () => {
  await client?.close();
});

/** Poll a subprocess's folded result until exit_code/signal leave the
 * -1 (still running) sentinel. */
async function waitSubprocessTerminal(operationId, spid, deadlineSeconds = 30) {
  const deadline = performance.now() / 1000 + deadlineSeconds;
  for (;;) {
    const result = await client.operationSubprocess(operationId, spid);
    const state = result.state;
    if (state && (state.exit_code !== -1 || state.signal !== -1)) {
      return result;
    }
    if (performance.now() / 1000 > deadline) {
      assert.fail(
        `subprocess ${spid} of ${operationId} did not finish in time`,
      );
    }
    await sleep(1);
  }
}

/** Wait until the parent instance is EXECUTING/ASSIGNED - a freshly
 * spawned operation is 202 Accepted, not yet a live exec target. */
async function waitRunning(operationId, deadlineSeconds = 30) {
  const deadline = performance.now() / 1000 + deadlineSeconds;
  for (;;) {
    const status = (
      await client.getOperationStatus(operationId, { inflight: true })
    ).status;
    if (
      status === OperationStatus.EXECUTING ||
      status === OperationStatus.ASSIGNED
    ) {
      return;
    }
    assert.ok(
      !TERMINAL_STATUSES.has(status),
      `operation ${operationId} finished before going EXECUTING: ${status}`,
    );
    if (performance.now() / 1000 > deadline) {
      assert.fail(`operation ${operationId} never reached EXECUTING/ASSIGNED`);
    }
    await sleep(1);
  }
}

async function pickSampleImage() {
  const images = (await client.listImages({ tagged: true, limit: 100 })).images;
  const busybox = images.find((item) => item.tag?.includes("busybox"));
  if (busybox) {
    return busybox;
  }
  // no busybox tag: some catalogs carry stale entries whose object
  // storage sync never completed, so prefer the first candidate that
  // is demonstrably real (a cheap checkImageFile probe) over images[0]
  // blindly - falls back to images[0] if none check out
  for (const candidate of images) {
    if (await client.checkImageFile(String(candidate.uuid), "/etc/passwd")) {
      return candidate;
    }
  }
  return images[0];
}

test("whoami reports permissions and limits", { skip }, async () => {
  const me = await client.whoami();
  assert.ok(me.token_uuid);
  assert.ok(Object.keys(me.permissions).length > 0);
});

test("read-only listings work end to end", { skip }, async () => {
  const images = (await client.listImages({ tagged: true, limit: 100 })).images;
  assert.ok(Array.isArray(images) && images.length > 0);
  for await (const summary of client.iterOperations({
    page_size: 20,
    limit: 20,
  })) {
    assert.ok(summary.uuid);
  }
});

test("inspect image grep finds and misses (read-only)", { skip }, async (t) => {
  const image = await pickSampleImage();
  if (
    !image ||
    !(await client.checkImageFile(String(image.uuid), "/etc/passwd"))
  ) {
    t.skip("no sample image with /etc/passwd in this namespace");
    return;
  }

  const result = await client.inspectImageGrep(String(image.uuid), "root", {
    path: "/etc/passwd",
  });
  assert.equal(result.path, "/etc/passwd");
  assert.ok(result.patterns.includes("root"));
  assert.ok(result.matches.length > 0);
  assert.ok(result.matches.every((match) => match.path.endsWith("passwd")));

  const absent = await client.inspectImageGrep(
    String(image.uuid),
    `definitely-not-there-${randomUUID()}`,
    { path: "/etc/passwd" },
  );
  assert.deepEqual(absent.matches, []);
});

test("spawn, wait and typed events (one spawn)", { skip }, async (t) => {
  if (!permissions.spawn_disposable && !permissions.spawn) {
    t.skip("token lacks spawn permissions");
    return;
  }
  const image = await pickSampleImage();

  const spawned = await client.spawnInstance(
    "echo js-live-smoke",
    String(image.uuid),
    { shell: true, disposable: true, timeout: 60 },
  );
  const operation = await client.waitOperation(spawned.uuid, { timeout: 180 });
  assert.equal(operation.status, OperationStatus.SUCCESS);
  assert.match(operation.metadata.result.stdout.asText(), /js-live-smoke/);

  // replay the finished event log (read-only)
  const types = [];
  for await (const event of client.iterOperationEvents(spawned.uuid)) {
    types.push(event.type);
  }
  assert.ok(types.includes("exit"));
});

test(
  "exec, stdin and kill subprocesses inside a running instance (one spawn)",
  { skip },
  async (t) => {
    if (!permissions.spawn_disposable && !permissions.spawn) {
      t.skip("token lacks spawn permissions");
      return;
    }
    const image = await pickSampleImage();
    const marker = `js-live-subprocess-${randomUUID().slice(0, 8)}`;

    // a parent instance that stays EXECUTING long enough to host
    // three execs in turn (killing a non-1 spid does not affect the
    // parent, but nothing here depends on that either way)
    const spawned = await client.spawnInstance("sleep 90", String(image.uuid), {
      shell: true,
      disposable: true,
      timeout: 120,
    });
    const operationId = spawned.uuid;
    try {
      await waitRunning(operationId);

      // -- exec a quick subprocess and read its folded result --
      const spid = await client.operationSubprocessCreate(
        operationId,
        `echo ${marker}`,
        { shell: true },
      );
      assert.ok(spid >= 2);
      const result = await waitSubprocessTerminal(operationId, spid);
      assert.equal(result.state.exit_code, 0);
      assert.match(result.stdout.asText(), new RegExp(marker));

      // -- write to a subprocess's stdin out-of-band, then close it --
      const catSpid = await client.operationSubprocessCreate(
        operationId,
        "cat",
        {
          stdin: new ClosableStreamRepr({ value: "", close: false }),
        },
      );
      await client.operationSubprocessStdin(
        operationId,
        catSpid,
        `${marker}-stdin\n`,
        { close: true },
      );
      const catResult = await waitSubprocessTerminal(operationId, catSpid);
      assert.match(catResult.stdout.asText(), new RegExp(`${marker}-stdin`));

      // -- kill a long-lived subprocess --
      const killSpid = await client.operationSubprocessCreate(
        operationId,
        "sleep 60",
        { shell: true },
      );
      await client.operationSubprocessKill(operationId, killSpid, {
        signal: "TERM",
      });
      const killed = await waitSubprocessTerminal(operationId, killSpid);
      // a killed process reports its signal (exit_code stays -1)
      assert.ok(killed.state.signal && killed.state.signal !== 0);
    } finally {
      // best-effort cleanup: the parent may already be terminal by
      // now (natural completion or the kill's side effect above), in
      // which case cancelling it 409s - not a test failure
      if (permissions.cancel) {
        try {
          await client.cancelOperation(operationId);
        } catch (error) {
          if (error?.status !== 409) {
            throw error;
          }
        }
      }
    }
  },
);
