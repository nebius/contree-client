"""Live JavaScript scenarios, executed by pytest through jsrun."""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

CLIENT_MODULE = (Path(__file__).resolve().parents[1] / "lib" / "index.js").as_uri()

COMMON = r"""
import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import { setTimeout as sleep } from "node:timers/promises";
import {
  ContreeClient,
  ClosableStreamRepr,
  OperationStatus,
  TERMINAL_STATUSES,
  profiles,
} from CLIENT_MODULE;

const profile = profiles.fromEnv();
assert.ok(profile, "CONTREE_TOKEN and CONTREE_URL are required");

const client = new ContreeClient(profile.token, {
  baseUrl: profile.url,
  project: profile.project,
  identity: "contree-client-js-ci/0",
});

async function waitSubprocessTerminal(operationId, spid) {
  const deadline = performance.now() + 30_000;

  for (;;) {
    const result = await client.operationSubprocess(operationId, spid);
    const state = result.state;
    if (state && (state.exitCode !== -1 || state.signal !== -1)) {
      return result;
    }

    assert.ok(
      performance.now() < deadline,
      `subprocess ${spid} of ${operationId} did not finish in time`,
    );
    await sleep(1000);
  }
}

async function waitRunning(operationId) {
  const deadline = performance.now() + 30_000;

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
    assert.ok(
      performance.now() < deadline,
      `operation ${operationId} never reached EXECUTING/ASSIGNED`,
    );
    await sleep(1000);
  }
}

async function pickSampleImage() {
  const images = (await client.listImages({ tagged: true, limit: 100 })).images;
  const busybox = images.find((item) => item.tag?.includes("busybox"));
  if (busybox) {
    return busybox;
  }

  for (const candidate of images) {
    if (await client.checkImageFile(String(candidate.uuid), "/etc/passwd")) {
      return candidate;
    }
  }

  return images[0];
}

async function run(permissions) {
  SCENARIO
}

try {
  const permissions = (await client.whoami()).permissions ?? {};
  const reason = await run(permissions);
  console.log(JSON.stringify({ skip: reason ?? "" }));
} finally {
  await client.close();
}
"""


@pytest.fixture
def live_js(jsrun: Callable[[str], str]) -> Callable[[str], None]:
    if not (os.environ.get("CONTREE_TOKEN") or os.environ.get("NEBIUS_API_KEY")):
        pytest.skip("JS live tests require CONTREE_TOKEN or NEBIUS_API_KEY")
    if not os.environ.get("CONTREE_URL"):
        pytest.fail("JS live tests require an explicit CONTREE_URL")

    def run(source: str) -> None:
        program = COMMON.replace("CLIENT_MODULE", json.dumps(CLIENT_MODULE)).replace(
            "SCENARIO", source
        )
        result = json.loads(jsrun(program))
        if result["skip"]:
            pytest.skip(result["skip"])

    return run


def test_js_whoami(live_js: Callable[[str], None]) -> None:
    live_js(r"""
  const me = await client.whoami();
  assert.ok(me.tokenUuid);
  assert.ok(Object.keys(me.permissions).length > 0);
    """)


def test_js_lists(live_js: Callable[[str], None]) -> None:
    live_js(r"""
  const images = (await client.listImages({ tagged: true, limit: 100 })).images;
  assert.ok(Array.isArray(images) && images.length > 0);

  for await (const summary of client.iterOperations({ pageSize: 20, limit: 20 })) {
    assert.ok(summary.uuid);
  }
    """)


def test_js_grep(live_js: Callable[[str], None]) -> None:
    live_js(r"""
  const image = await pickSampleImage();
  if (
    !image ||
    !(await client.checkImageFile(String(image.uuid), "/etc/passwd"))
  ) {
    return "no sample image with /etc/passwd in this namespace";
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
    """)


def test_js_spawn_wait_and_events(live_js: Callable[[str], None]) -> None:
    live_js(r"""
  if (!permissions.spawn_disposable && !permissions.spawn) {
    return "token lacks spawn permissions";
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

  const types = [];
  for await (const event of client.iterOperationEvents(spawned.uuid)) {
    types.push(event.type);
  }
  assert.ok(types.includes("exit"));
    """)


def test_js_subprocess_stdin_and_kill(live_js: Callable[[str], None]) -> None:
    live_js(r"""
  if (!permissions.spawn_disposable && !permissions.spawn) {
    return "token lacks spawn permissions";
  }
  const image = await pickSampleImage();
  const marker = `js-live-subprocess-${randomUUID().slice(0, 8)}`;

  const spawned = await client.spawnInstance("sleep 90", String(image.uuid), {
    shell: true,
    disposable: true,
    timeout: 120,
  });
  const operationId = spawned.uuid;

  try {
    await waitRunning(operationId);

    const spid = await client.operationSubprocessCreate(
      operationId,
      `echo ${marker}`,
      { shell: true },
    );
    assert.ok(spid >= 2);

    const result = await waitSubprocessTerminal(operationId, spid);
    assert.equal(result.state.exitCode, 0);
    assert.match(result.stdout.asText(), new RegExp(marker));

    const catSpid = await client.operationSubprocessCreate(operationId, "cat", {
      stdin: new ClosableStreamRepr({ value: "", close: false }),
    });
    await client.operationSubprocessStdin(
      operationId,
      catSpid,
      `${marker}-stdin\n`,
      { close: true },
    );

    const catResult = await waitSubprocessTerminal(operationId, catSpid);
    assert.match(catResult.stdout.asText(), new RegExp(`${marker}-stdin`));

    const killSpid = await client.operationSubprocessCreate(
      operationId,
      "sleep 60",
      { shell: true },
    );
    await client.operationSubprocessKill(operationId, killSpid, {
      signal: "TERM",
    });

    const killed = await waitSubprocessTerminal(operationId, killSpid);
    assert.ok(killed.state.signal && killed.state.signal !== 0);
  } finally {
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
    """)
