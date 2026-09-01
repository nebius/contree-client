import assert from "node:assert/strict";
import { after, before, test } from "node:test";

import { ContreeClient } from "../lib/client.js";
import { BadRequestError, NotFoundError } from "../lib/errors.js";
import {
  File,
  FileResponse,
  Image,
  OperationInstanceMetadata,
  OperationResponse,
  OperationStatus,
} from "../lib/models.js";
import { RetryPolicy, sha256, textToBytes } from "../lib/runtime.js";
import {
  FLAKY_OPERATION_UUID,
  IMAGE_UUID,
  KNOWN_SHA256,
  OPERATION_UUID,
  RETRY_AFTER_OPERATION_UUID,
  startStub,
} from "./stub.mjs";

let stub;
let client;

before(async () => {
  stub = await startStub();
  client = new ContreeClient("test-token", {
    baseUrl: stub.baseUrl,
    project: "test-project",
  });
});

after(async () => {
  await client.close();
  stub.stop();
});

test("whoami returns the token introspection", async () => {
  const response = await client.whoami();
  assert.equal(response.token_uuid, "a1b2c3d4-e5f6-7890-abcd-ef1234567890");
  assert.equal(response.permissions.spawn, true);
});

test("rejects an invalid baseUrl scheme", () => {
  assert.throws(
    () => new ContreeClient("tok", { baseUrl: "ftp://example.com" }),
    RangeError,
  );
});

test("rejects credentials in baseUrl", () => {
  assert.throws(
    () =>
      new ContreeClient("tok", {
        baseUrl: "http://user:password@example.com",
      }),
    /baseUrl must not include credentials/,
  );
});

test("rejects invalid headers before fetch", async () => {
  let fetchCalls = 0;
  const invalid = new ContreeClient("bad\ntoken", {
    baseUrl: "http://127.0.0.1:1",
    retry: new RetryPolicy({ delays: [0], maxAttempts: 3 }),
    fetch: async () => {
      fetchCalls += 1;
      throw new TypeError("network failure");
    },
  });

  await assert.rejects(
    invalid.call({ method: "GET", path: "/x", idempotent: true }),
    /invalid HTTP header name or value/,
  );
  assert.equal(fetchCalls, 0);
});

test("operation status is a typed model", async () => {
  const operation = await client.getOperationStatus(OPERATION_UUID);
  assert.ok(operation instanceof OperationResponse);
  assert.equal(operation.status, OperationStatus.SUCCESS);
  assert.ok(operation.metadata instanceof OperationInstanceMetadata);
  assert.equal(operation.metadata.result.stdout.asText(), "hi\n");
});

test("spawnInstance posts the body and returns the uuid", async () => {
  const response = await client.spawnInstance("echo hi", "tag:busybox:latest", {
    shell: true,
    env: { KEY: "value" },
    timeout: 30,
  });
  assert.equal(response.uuid, OPERATION_UUID);
});

test("listImages returns typed models, bad limit raises 400", async () => {
  const listed = await client.listImages();
  assert.ok(listed.images[0] instanceof Image);
  assert.equal(listed.images[0].uuid, IMAGE_UUID);
  await assert.rejects(client.listImages({ limit: 0 }), BadRequestError);
});

test("iterImages paginates transparently and honors limit", async () => {
  const items = [];
  for await (const image of client.iterImages({
    tag: "paginated",
    page_size: 2,
  })) {
    items.push(image);
  }
  assert.equal(items.length, 5);
  assert.ok(items.every((image) => image instanceof Image));

  const bounded = [];
  for await (const image of client.iterImages({
    tag: "paginated",
    page_size: 2,
    limit: 3,
  })) {
    bounded.push(image);
  }
  assert.equal(bounded.length, 3);
  await assert.rejects(async () => {
    for await (const unused of client.iterImages({ page_size: 0 })) {
      void unused;
    }
  }, RangeError);
});

test("file upload, dedup probe and ensureFile", async () => {
  const payload = textToBytes("hello world\n");
  const uploaded = await client.uploadFile(payload);
  assert.ok(uploaded instanceof FileResponse);
  assert.equal(uploaded.sha256, await sha256(payload));

  assert.equal(await client.checkFileExists(KNOWN_SHA256), true);
  assert.equal(await client.checkFileExists("b".repeat(64)), false);

  const known = await client.getFile(KNOWN_SHA256);
  assert.ok(known instanceof File);
  await assert.rejects(client.getFile("b".repeat(64)), NotFoundError);

  // the payload is unknown server-side: ensureFile falls back to upload
  const ensured = await client.ensureFile(payload);
  assert.equal(ensured.sha256, await sha256(payload));
});

test("findImageByTag follows the redirect to the image uuid", async () => {
  const uuid = await client.inspectFindImageByTag("busybox:latest");
  assert.equal(uuid, IMAGE_UUID);
});

test("resolveImage accepts uuid, tag: prefix and bare tags", async () => {
  assert.equal(await client.resolveImage(IMAGE_UUID), IMAGE_UUID);
  assert.equal(await client.resolveImage("tag:busybox:latest"), IMAGE_UUID);
  assert.equal(await client.resolveImage("busybox:latest"), IMAGE_UUID);
});

test("retry policy retries 5xx and honors Retry-After", async () => {
  const retrying = new ContreeClient("test-token", {
    baseUrl: stub.baseUrl,
    retry: new RetryPolicy({ delays: [0] }),
  });
  const flaky = await retrying.getOperationStatus(FLAKY_OPERATION_UUID);
  assert.equal(flaky.uuid, FLAKY_OPERATION_UUID);
  const delayed = await retrying.getOperationStatus(RETRY_AFTER_OPERATION_UUID);
  assert.equal(delayed.uuid, RETRY_AFTER_OPERATION_UUID);
  await retrying.close();
});

test("425/429 replay a POST even without retryUnsafe", async () => {
  // both mean the backend rejected the request before any processing
  // (a documented contract, not just RFC 8470 for 425) - the stub
  // fails the first attempt only, so success proves the client retried
  const retrying = new ContreeClient("test-token", {
    baseUrl: stub.baseUrl,
    retry: new RetryPolicy({ delays: [0] }),
  });
  const early = await retrying.spawnInstance("flaky-425", "tag:busybox:latest");
  assert.equal(early.uuid, OPERATION_UUID);
  const throttled = await retrying.spawnInstance(
    "flaky-429",
    "tag:busybox:latest",
  );
  assert.equal(throttled.uuid, OPERATION_UUID);
  await retrying.close();
});

test("a null token sends no Authorization header", () => {
  const spec = { method: "GET", path: "/health" };
  const anonymous = new ContreeClient(null, {
    baseUrl: "http://localhost:1",
  });
  assert.equal(anonymous.token, null);
  assert.equal("Authorization" in anonymous.buildHeaders(spec), false);
  const authorized = new ContreeClient("tok", {
    baseUrl: "http://localhost:1",
  });
  assert.equal(authorized.buildHeaders(spec).Authorization, "Bearer tok");
});

test("userAgent carries identity first", () => {
  const identified = new ContreeClient("tok", {
    baseUrl: "http://localhost:1",
    identity: "my-app/9.9",
  });
  assert.ok(identified.userAgent().startsWith("my-app/9.9 contree-client-js/"));
});
