/** Fixtures for docs/js/*.md's doc tests (see docs.test.mjs).
 *
 * Mirrors root conftest.py's `arm()`: each fixture is a zero-arg
 * factory so a doc test gets a fresh instance, and `client` is a
 * `contree-client/testing` double pre-armed with a canned happy-path
 * response for every operation any annotated doc example calls - the
 * doc's own code only needs to re-arm what it specifically wants to
 * demonstrate.
 */

import { createHash } from "node:crypto";

import {
  DirectoryList,
  File,
  FilesListResponse,
  Image,
  ImageListResponse,
  InstanceSpawnResponse,
  OperationEvent,
  OperationResponse,
  OperationSummary,
  WhoAmIResponse,
} from "../lib/models.js";
import { NotFoundError } from "../lib/errors.js";
import { ContreeClient } from "contree-client/testing";
import { FILE_UUID, IMAGE_UUID, OPERATION_UUID } from "./stub.mjs";

const PAYLOAD = "hello world\n";
const PAYLOAD_SHA256 = createHash("sha256").update(PAYLOAD).digest("hex");
const CREATED_AT = "2024-01-01T12:00:00+00:00";

const FILE_INFO = {
  uuid: FILE_UUID,
  sha256: PAYLOAD_SHA256,
  size: PAYLOAD.length,
  created_at: CREATED_AT,
  updated_at: CREATED_AT,
};

const EXIT_DATA = {
  pid: 42,
  code: 0,
  signal: -1,
  timed_out: false,
  duration_ms: 12,
  resources: {
    user_time_us: 1000,
    sys_time_us: 500,
    max_rss_kb: 1024,
    shared_memory: 0,
    unshared_memory: 0,
    swaps: 0,
    minor_faults: 0,
    major_faults: 0,
    voluntary_ctx_switches: 0,
    involuntary_ctx_switches: 0,
    block_input_ops: 0,
    block_output_ops: 0,
    ipc_msgs_sent: 0,
    ipc_msgs_received: 0,
    signals_received: 0,
  },
};

const EVENT_PAYLOADS = [
  {
    id: 0,
    ts: "2026-06-08T20:00:00Z",
    spid: 0,
    type: "init",
    data: {
      started_at: "2026-06-08T20:00:00.000000000Z",
      runtime_path: "/run/contreeinitd",
      verbose: false,
      init_pid: 1,
    },
  },
  {
    id: 1,
    ts: "2026-06-08T20:00:00.10Z",
    spid: 1,
    type: "spawn",
    data: {
      pid: 42,
      command: "/bin/sh",
      args: ["-c", "echo hello world"],
      shell: true,
      cwd: "/",
      uid: 0,
      gid: 0,
      timeout: 60,
      truncate_at: 1048576,
      env: { PATH: "/usr/bin:/bin" },
    },
  },
  {
    id: 2,
    ts: "2026-06-08T20:00:00.50Z",
    spid: 1,
    type: "stdout",
    data: { value: "hello world\n", encoding: "ascii" },
  },
  {
    id: 3,
    ts: "2026-06-08T20:00:00.60Z",
    spid: 1,
    type: "truncated",
    data: { stream: "stdout", bytes_emitted: 1048576, bytes_dropped: 4096 },
  },
  {
    id: 4,
    ts: "2026-06-08T20:00:01Z",
    spid: 1,
    type: "exit",
    data: EXIT_DATA,
  },
  {
    id: 5,
    ts: "2026-06-08T20:00:02Z",
    spid: 0,
    type: "completion",
    data: { status: "SUCCESS", error: null, duration_ms: 1500 },
  },
];

const OPERATION_PAYLOAD = {
  uuid: OPERATION_UUID,
  kind: "instance",
  status: "SUCCESS",
  metadata: {
    command: "echo hello world",
    image: "tag:ubuntu:latest",
    result: {
      state: { exit_code: 0, pid: 42 },
      stdout: { value: "hello world\n", encoding: "ascii" },
      stderr: { value: "", encoding: "ascii" },
    },
  },
  result_image_uuid: IMAGE_UUID,
};

function buildTar() {
  // a minimal, valid, single-file POSIX tar (no padding beyond the
  // required two 512-byte zero blocks) - enough for a doc example that
  // only demonstrates streaming the chunks, never unpacks them
  const body = Buffer.from("127.0.0.1 localhost\n");
  const header = Buffer.alloc(512);
  header.write("etc/hosts", 0);
  header.write((body.length.toString(8) + "\0").padStart(12, "0"), 124);
  const padded = Buffer.concat([body]);
  const pad = (512 - (padded.length % 512)) % 512;
  return Buffer.concat([header, padded, Buffer.alloc(pad), Buffer.alloc(1024)]);
}

/** A `contree-client/testing` double pre-armed with a canned
 * happy-path response for every operation a doc example calls.
 *
 * Unlike the Python double, this one wraps every public method
 * uniformly - a composite method (`waitOperation`, `iterImages`, ...)
 * does not fall through to the primitives it would call for real
 * (`getOperationStatus`, `listImages`, ...), so each needs its own
 * direct mock alongside the primitive it mirrors.
 */
function armedClient() {
  const client = new ContreeClient();
  client.mock(
    "whoami",
    WhoAmIResponse.fromWire({
      token_uuid: "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
      token_expiration: null,
      permissions: { spawn: true, import: true },
      limits: { instance_max_timeout: 3600 },
      operations_stat: {},
    }),
  );
  client.mock(
    "spawnInstance",
    InstanceSpawnResponse.fromWire({ uuid: OPERATION_UUID }),
  );
  const operation = OperationResponse.fromWire(OPERATION_PAYLOAD);
  client.mock("getOperationStatus", operation);
  client.mock("waitOperation", operation);
  const events = EVENT_PAYLOADS.map((event) => OperationEvent.fromWire(event));
  client.mock("iterOperationEvents", events);
  client.mock("followOperationEvents", events);
  client.mock("cancelOperation", null);
  const operationSummaries = [
    { uuid: OPERATION_UUID, kind: "instance", status: "SUCCESS" },
  ].map((item) => OperationSummary.fromWire(item));
  client.mock("listOperations", operationSummaries);
  client.mock("iterOperations", operationSummaries);
  client.mock(
    "uploadFile",
    File.fromWire({ ...FILE_INFO, size: PAYLOAD.length }),
  );
  client.mock("getFile", File.fromWire(FILE_INFO));
  client.mock("ensureFile", File.fromWire(FILE_INFO));
  client.mock("checkFileExists", true);
  client.mock("listFiles", FilesListResponse.fromWire({ files: [FILE_INFO] }));
  client.mock("iterFiles", [File.fromWire(FILE_INFO)]);
  const imageWire = { uuid: IMAGE_UUID, tag: "busybox:latest" };
  client.mock(
    "listImages",
    ImageListResponse.fromWire({ images: [imageWire] }),
  );
  client.mock("iterImages", [Image.fromWire(imageWire)]);
  client.mock("importImage", OPERATION_UUID);
  client.mock("resolveImage", IMAGE_UUID);
  client.mock(
    "updateImageTag",
    Image.fromWire({ uuid: IMAGE_UUID, tag: "my/base:latest" }),
  );
  client.mock("deleteImageTag", null);
  client.mock("inspectFindImageByTag", IMAGE_UUID);
  client.mock(
    "inspectImage",
    Image.fromWire({ uuid: IMAGE_UUID, tag: "busybox:latest" }),
  );
  client.mock(
    "inspectImageList",
    DirectoryList.fromWire({ path: "/etc", files: [] }),
  );
  client.mock(
    "inspectImageDownload",
    new TextEncoder().encode("127.0.0.1 localhost\n"),
  );
  client.mock("inspectImageDownloadStream", [
    new TextEncoder().encode("127.0.0.1 "),
    new TextEncoder().encode("localhost\n"),
  ]);
  client.mock("checkImageFile", true);
  client.mock("checkImageArchive", true);
  client.mock("inspectImageArchive", [buildTar()]);
  return client;
}

/** Like `armedClient()`, but `inspectImage` rejects with a
 * `NotFoundError` instead of succeeding - for the "handle errors" doc
 * example, bound under the `client` identifier via `fixtures:
 * notFoundClient as client`. */
function notFoundClient() {
  const client = armedClient();
  client.mock("inspectImage", null, {
    error: new NotFoundError(404, "no such image"),
  });
  return client;
}

/** Fixture factories, keyed by the name a doc's `fixtures:` annotation
 * requests. Each is a zero-arg function so a doc test gets a fresh
 * value, matching the pytest fixtures' per-test scoping. */
export const fixtures = {
  client: armedClient,
  notFoundClient,
  // a doc example's "write it wherever you like" destination
  sink: () => ({ write: () => {} }),
  tarSink: () => ({ write: () => {} }),
  token: () => "test-token",
  // a doc example that only constructs a client, never calls it
  customFetch: () => () => {},
  operationId: () => OPERATION_UUID,
  imageUuid: () => IMAGE_UUID,
  fileUuid: () => FILE_UUID,
  payload: () => PAYLOAD,
  // testing.md's own walkthrough of arming a fresh double by hand
  codeUnderTest: () => (client) =>
    client.spawnInstance("echo hi", "tag:busybox:latest"),
  executing: () => ({ status: "EXECUTING" }),
  success: () => ({ status: "SUCCESS" }),
  initEvent: () => ({ id: 0, spid: 0, type: "init" }),
  exitEvent: () => ({ id: 1, spid: 1, type: "exit" }),
};
