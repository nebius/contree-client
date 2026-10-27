import assert from "node:assert/strict";
import { test } from "node:test";

import {
  EventDataInspect,
  EventDataFileUpload,
  EventInspectParameters,
  ExecSpec,
  OperationEvent,
  ProcessResourcesLimits,
} from "../lib/models.js";

test("live instance events decode typed payloads and preserve wire fields", () => {
  const cases = [
    [
      "inspect",
      EventDataInspect,
      {
        request_id: "rpc-1",
        method: "upload",
        phase: "started",
        params: {
          path: "/workspace",
          file_uuid: "file-1",
          unpack: true,
          replace: true,
          merge: false,
        },
      },
    ],
    [
      "inspect",
      EventDataInspect,
      {
        request_id: "rpc-1",
        method: "upload",
        phase: "completed",
        status: "success",
        error: null,
        handle: "upload-1",
      },
    ],
    [
      "file_upload",
      EventDataFileUpload,
      {
        handle: "upload-1",
        file_uuid: "file-1",
        path: "/workspace",
        unpack: true,
        replace: true,
        merge: false,
        bytes: 512,
        status: "success",
        error: null,
      },
    ],
  ];
  for (const [type, Model, data] of cases) {
    const event = OperationEvent.fromWire({
      id: 1,
      ts: "2026-10-27T12:00:00Z",
      spid: 0,
      type,
      data,
    });
    assert.ok(event.data instanceof Model);
    assert.deepEqual(event.toWire().data, data);
    if (data.params) {
      assert.ok(event.data.params instanceof EventInspectParameters);
      assert.equal(event.data.params.merge, false);
    }
  }
  const incomplete = { handle: "incomplete" };
  const event = OperationEvent.fromWire({
    id: 1,
    ts: "2026-10-27T12:00:00Z",
    type: "file_upload",
    data: incomplete,
  });
  assert.deepEqual(event.data, incomplete);
});

test("process resource limits preserve absent, null and explicit values", () => {
  for (const limits of [
    {},
    { cpu: null, memory_mib: null },
    { cpu: 1, memory_mib: 256 },
  ]) {
    const wire = { command: "/bin/true", resources_limits: limits };
    const model = ExecSpec.fromWire(wire);
    assert.ok(model.resourcesLimits instanceof ProcessResourcesLimits);
    assert.deepEqual(model.toWire(), wire);
  }
});
