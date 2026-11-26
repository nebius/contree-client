import assert from "node:assert/strict";
import { test } from "node:test";

import {
  buildIterOperationEvents,
  buildListImages,
  buildSpawnInstance,
} from "../lib/operations.js";
import { IMAGE_UUID, OPERATION_UUID } from "./stub.mjs";

for (const [name, build] of [
  ["list images", (options) => buildListImages(options)],
  ["spawn instance", (options) => buildSpawnInstance(IMAGE_UUID, options)],
  [
    "operation events",
    (options) => buildIterOperationEvents(OPERATION_UUID, options),
  ],
]) {
  test(`${name} rejects invalid options shapes and unknown names`, () => {
    assert.throws(() => build(null), {
      name: "TypeError",
      message: "options must be a non-null object, not an array",
    });
    assert.throws(() => build([]), {
      name: "TypeError",
      message: "options must be a non-null object, not an array",
    });
    assert.throws(() => build({ typo: true }), {
      name: "TypeError",
      message: "unknown option: typo",
    });
  });
}

test("known query options preserve their wire values", () => {
  const request = buildListImages({ limit: 7, offset: 2 });

  assert.equal(request.query.limit, "7");
  assert.equal(request.query.offset, "2");
});

test("body options use public camelCase names", () => {
  const request = buildSpawnInstance(IMAGE_UUID, { keepAlive: true });

  assert.equal(JSON.parse(request.body).keep_alive, true);
  assert.throws(() => buildSpawnInstance(IMAGE_UUID, { keep_alive: true }), {
    name: "TypeError",
    message: "unknown option: keep_alive",
  });
});

test("event stream controls remain accepted without becoming wire parameters", () => {
  const request = buildIterOperationEvents(OPERATION_UUID, {
    follow: true,
    lastEventId: 12,
    deadline: 123,
    idleTimeout: 5,
    _singleAttempt: true,
  });

  assert.deepEqual(request.query, { follow: "1" });
  assert.deepEqual(request.headers, { "Last-Event-Id": "12" });
});

test("stream-only controls are rejected by other operations", () => {
  assert.throws(() => buildListImages({ idleTimeout: 5 }), {
    name: "TypeError",
    message: "unknown option: idleTimeout",
  });
});
