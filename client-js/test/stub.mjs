/** Spawns the repository's canned-response stub server for the JS
 * test suites. The stub is stdlib-only python; it prints its base URL
 * as the first stdout line and exits when stdin closes. Set
 * CONTREE_STUB_URL to reuse an externally managed instance. */

import { spawn } from "node:child_process";
import { once } from "node:events";
import { createInterface } from "node:readline";
import { fileURLToPath } from "node:url";

const REPO_ROOT = fileURLToPath(new URL("../..", import.meta.url));

export async function startStub() {
  const external = process.env.CONTREE_STUB_URL;
  if (external) {
    return { baseUrl: external, stop() {} };
  }
  const python = process.env.PYTHON ?? "python3";
  const child = spawn(python, ["client/tests/stub_server.py"], {
    cwd: REPO_ROOT,
    stdio: ["pipe", "pipe", "inherit"],
  });
  const lines = createInterface({ input: child.stdout });
  const [baseUrl] = await once(lines, "line");
  // the child must not keep the parent's event loop alive: Node
  // 18.17's test runner never fires the root after() hook, so the
  // suite would hang waiting for these handles. The stub exits on
  // stdin EOF, which parent exit delivers for free.
  lines.close();
  child.stdout.destroy();
  child.stdin.unref();
  child.unref();
  return {
    baseUrl,
    stop() {
      child.stdin.end();
      child.kill();
    },
  };
}

// mirrors client/tests/stub_server.py
export const IMAGE_UUID = "12345678-9abc-baba-deda-0123456789ab";
export const OPERATION_UUID = "87654321-9abc-baba-deda-0123456789ab";
export const FLAKY_OPERATION_UUID = "00000000-0000-0000-0000-00000000f1a2";
export const RETRY_AFTER_OPERATION_UUID =
  "00000000-0000-0000-0000-000000000425";
export const RECONNECT_OPERATION_UUID = "00000000-0000-0000-0000-00000000ec0e";
export const RESET_OPERATION_UUID = "00000000-0000-0000-0000-000000000e5e";
export const EVENTS_UNAVAILABLE_OPERATION_UUID =
  "00000000-0000-0000-0000-00000000eee0";
export const EVENTS_UNAVAILABLE_STALLED_OPERATION_UUID =
  "00000000-0000-0000-0000-00000000eee1";
export const TRUNCATED_IMAGE_UUID = "00000000-0000-0000-0000-000000000cec";
export const FILE_UUID = "a9165a5d-5c86-4bd8-8ee4-ae46c19cf45d";
export const KNOWN_SHA256 = "a".repeat(64);
export const DOWNLOAD_CONTENT = "127.0.0.1 localhost\n";
