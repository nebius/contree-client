/** Live smoke against a real Contree API.
 *
 * Runs only when the environment describes a profile (CONTREE_TOKEN /
 * CONTREE_URL, see profiles.fromEnv()); performs exactly ONE spawn
 * and no imports - the CI token allows 8 concurrent runs and a single
 * concurrent import, and the workflow serializes live runs anyway.
 * Never point these variables at production.
 */

import assert from "node:assert/strict";
import { after, before, test } from "node:test";

import { ContreeClient } from "../../lib/client.js";
import { OperationStatus } from "../../lib/models.js";
import { fromEnv } from "../../lib/profiles.js";

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

test("spawn, wait and typed events (one spawn)", { skip }, async (t) => {
  if (!permissions.spawn_disposable && !permissions.spawn) {
    t.skip("token lacks spawn permissions");
    return;
  }
  const images = (await client.listImages({ tagged: true, limit: 100 })).images;
  const image =
    images.find((item) => item.tag?.includes("busybox")) ?? images[0];

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
