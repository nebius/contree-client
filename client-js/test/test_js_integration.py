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
  EventDataInspect,
  EventDataFileUpload,
  InstanceResourcesLimits,
  OperationStatus,
  SSEParser,
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
  assert.ok(SAMPLE_IMAGE_UUID, "the session image fixture is required");
  return { uuid: SAMPLE_IMAGE_UUID };
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
def live_js(jsrun: Callable[[str], str]) -> Callable[..., None]:
    if not (os.environ.get("CONTREE_TOKEN") or os.environ.get("NEBIUS_API_KEY")):
        pytest.skip("JS live tests require CONTREE_TOKEN or NEBIUS_API_KEY")
    if not os.environ.get("CONTREE_URL"):
        pytest.fail("JS live tests require an explicit CONTREE_URL")

    def run(source: str, *, image_uuid: str | None = None) -> None:
        program = (
            COMMON.replace("CLIENT_MODULE", json.dumps(CLIENT_MODULE))
            .replace("SCENARIO", source)
            .replace("SAMPLE_IMAGE_UUID", json.dumps(image_uuid))
        )
        result = json.loads(jsrun(program))
        if result["skip"]:
            pytest.skip(result["skip"])

    return run


@pytest.fixture
def live_js_image(
    live_js: Callable[..., None], live_image_uuid: str
) -> Callable[[str], None]:
    def run(source: str) -> None:
        live_js(source, image_uuid=live_image_uuid)

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
  assert.ok(Array.isArray(images));
  for (const image of images) assert.ok(image.uuid);

  for await (const summary of client.iterOperations({ pageSize: 20, limit: 20 })) {
    assert.ok(summary.uuid);
  }
    """)


def test_js_grep(live_js_image: Callable[[str], None]) -> None:
    live_js_image(r"""
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


def test_js_spawn_wait_and_events(live_js_image: Callable[[str], None]) -> None:
    live_js_image(r"""
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


def test_js_subprocess_stdin_and_kill(live_js_image: Callable[[str], None]) -> None:
    live_js_image(r"""
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


def test_js_live_instance_files_and_events(
    live_js_image: Callable[[str], None],
) -> None:
    live_js_image(r"""
  if (!permissions.spawn_disposable || !permissions.cancel) {
    return "token lacks disposable spawn or cancel permission";
  }
  const image = await pickSampleImage();
  const marker = `ready-${randomUUID()}`;
  const payload = Buffer.from(`live-file-${randomUUID()}\n`);
  const spawned = await client.spawnInstance(
    `echo ${marker}; read line`,
    String(image.uuid),
    {
      shell: true,
      disposable: true,
      timeout: 180,
      stdin: new ClosableStreamRepr({ value: "", close: false }),
      resourcesLimits: new InstanceResourcesLimits({ cpu: 2, memoryMib: 256 }),
    },
  );
  const id = spawned.uuid;

  try {
    let ready = false;
    let output = "";
    for await (const event of client.followOperationEvents(id, {
      timeout: 60,
    })) {
      if (event.type === "stdout") {
        output += event.data.asText();
        if (output.includes(marker)) {
          ready = true;
          break;
        }
      }
    }
    assert.ok(ready, "instance exited before readiness");

    const uploaded = await client.uploadFile(payload);
    const path = "/tmp/client-live/input.txt";
    await client.applyInstanceFile(id, uploaded.uuid, path, {
      unpack: false,
      replace: false,
      merge: false,
    });
    assert.equal(await client.checkInstanceFile(id, path), true);
    assert.equal(await client.checkInstanceFile(id, `${path}.missing`), false);
    assert.equal(
      await client.checkInstanceArchive(id, "/tmp/client-live"),
      true,
    );
    assert.equal(
      await client.checkInstanceArchive(id, "/tmp/client-live-missing"),
      false,
    );
    const listing = await client.inspectInstanceList(id, "/tmp/client-live");
    assert.ok(
      listing.files.some((file) => file.path.split("/").at(-1) === "input.txt"),
    );
    assert.deepEqual(
      Buffer.from(await client.inspectInstanceDownload(id, path)),
      payload,
    );

    const chunks = [];
    for await (const chunk of client.inspectInstanceDownloadStream(id, path))
      chunks.push(chunk);
    assert.deepEqual(Buffer.concat(chunks), payload);
    const grep = await client.inspectInstanceGrep(id, "live-file", {
      path: [path],
      glob: ["*.txt"],
    });
    assert.ok(
      grep.matches.some((match) =>
        match.lineText.includes(payload.toString().trim()),
      ),
    );

    const archive = [];
    for await (const chunk of client.inspectInstanceArchive(
      id,
      "/tmp/client-live",
    ))
      archive.push(chunk);
    assert.ok(Buffer.concat(archive).includes(payload));

    const replacement = Buffer.from(`replacement-${randomUUID()}\n`);
    const second = await client.uploadFile(replacement);
    await client.applyInstanceFile(id, second.uuid, path, { replace: true });
    assert.deepEqual(
      Buffer.from(await client.inspectInstanceDownload(id, path)),
      replacement,
    );

    await client.operationSubprocessStdin(id, 1, "done\n", { close: true });
    const finished = await client.waitOperation(id, { timeout: 60 });
    assert.equal(finished.status, OperationStatus.SUCCESS);
    const events = [];
    for await (const event of client.iterOperationEvents(id))
      events.push(event);
    const inspections = events.filter((event) => event.type === "inspect");
    assert.ok(inspections.length > 0);
    assert.ok(
      inspections.every(
        (event) => event.spid === 0 && event.data instanceof EventDataInspect,
      ),
    );
    for (const method of ["upload", "list", "open", "archive", "grep"]) {
      assert.ok(
        inspections.some((event) => event.data.method === method),
        method,
      );
    }
    const requests = new Set(
      inspections
        .filter((event) => event.data.phase === "started")
        .map((event) => event.data.requestId),
    );
    const results = new Set(
      inspections
        .filter((event) => event.data.phase === "completed")
        .map((event) => event.data.requestId),
    );
    assert.deepEqual(results, requests);
    const uploads = events
      .filter((event) => event.type === "file_upload")
      .map((event) => event.data);
    assert.equal(uploads.length, 2);
    assert.ok(
      uploads.every(
        (data) =>
          data instanceof EventDataFileUpload &&
          data.status === "success" &&
          data.error === null,
      ),
    );
    assert.deepEqual(
      new Set(uploads.map((data) => data.fileUuid)),
      new Set([uploaded.uuid, second.uuid]),
    );
    assert.deepEqual(
      uploads.map((data) => data.bytes),
      [payload.length, replacement.length],
    );
  } finally {
    const status = (await client.getOperationStatus(id, { inflight: true }))
      .status;
    if (!TERMINAL_STATUSES.has(status)) {
      await client.cancelOperation(id);
      await client.waitOperation(id, { timeout: 60, followEvents: false });
    }
  }
    """)


@pytest.mark.parametrize("ending", ["eof", "error"])
def test_js_recovers_retained_event_tail(
    live_js_image: Callable[[str], None], ending: str
) -> None:
    live_js_image(
        "const ending = "
        + json.dumps(ending)
        + ";"
        + r"""
  if (!permissions.spawn_disposable && !permissions.spawn) {
    return "token lacks spawn permissions";
  }
  const image = await pickSampleImage();
  const operation = await client.spawnInstance(
    "printf 'retained-one\n'; printf 'retained-two\n'",
    String(image.uuid),
    { shell: true, disposable: true, timeout: 30 },
  );
  const finished = await client.waitOperation(operation.uuid, {
    followEvents: false, timeout: 120,
  });
  assert.equal(finished.status, OperationStatus.SUCCESS);

  let reference;
  const requests = [];
  const interrupted = new ContreeClient(profile.token, {
    baseUrl: profile.url,
    project: profile.project,
    fetch: async (url, options) => {
      const parsed = new URL(url);
      const events = parsed.pathname.endsWith("/events");
      requests.push({
        events,
        follow: parsed.searchParams.get("follow"),
        cursor: new Headers(options.headers).get("Last-Event-Id"),
      });
      const response = await fetch(url, options);
      if (!events || requests.filter((request) => request.events).length !== 1) {
        return response;
      }
      assert.equal(response.status, 200);
      // Keep the real API's complete log as the reference, but expose only its
      // first payload frame to the follower before EOF or a transport error.
      const text = (await response.text()).replaceAll("\r\n", "\n");
      reference = new SSEParser().feed(new TextEncoder().encode(text))
        .filter((frame) => frame.data && frame.event !== "sse_error")
        .map((frame) => JSON.parse(frame.data).id);
      const blocks = text.split("\n\n");
      const first = blocks.findIndex((block) => /^data:/m.test(block));
      assert.ok(first >= 0);
      const prefix = blocks.slice(0, first + 1).join("\n\n") + "\n\n";
      const headers = new Headers(response.headers);
      headers.delete("content-encoding");
      headers.delete("content-length");
      const body = ending === "eof" ? prefix : new ReadableStream({
        start(controller) { controller.enqueue(new TextEncoder().encode(prefix)); },
        pull(controller) {
          controller.error(new TypeError("injected SSE disconnect"));
        },
      }, { highWaterMark: 0 });
      return new Response(body, { status: response.status, headers });
    },
  });
  try {
    const events = [];
    for await (const event of interrupted.followOperationEvents(operation.uuid, {
      timeout: 60, idleTimeout: 15,
    })) events.push(event);
    assert.ok(reference.length > 1);
    assert.deepEqual(events.map((event) => event.id), reference);
    assert.equal(events.at(-1).type, "completion");
    assert.deepEqual(requests.map((request) => request.events), [true, false, true]);
    assert.equal(requests[0].follow, "1");
    assert.notEqual(requests[2].follow, "1");
    assert.equal(requests[2].cursor, String(events[0].id));
  } finally {
    await interrupted.close();
  }
    """
    )
