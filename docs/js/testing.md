# Testing

`contree-client/testing` ships an offline double with the exact
generated client surface: every API method exists, records its calls
and replays outcomes you queue — no network, no mocks of your own.

<!-- name: testing_walkthrough; fixtures: operationId as OPERATION_UUID, codeUnderTest -->
```js
import assert from "node:assert/strict";
import { ContreeClient } from "contree-client/testing";
import { InstanceSpawnResponse } from "contree-client";

const client = new ContreeClient();
client.mock(
  "spawnInstance",
  InstanceSpawnResponse.fromWire({ uuid: OPERATION_UUID }),
);

await codeUnderTest(client); // accepts any ContreeClient

const calls = client.callsFor("spawnInstance");
assert.equal(calls.length, 1);
assert.equal(calls[0].args[0], "echo hi");
```

## Outcome queues

Mocked outcomes queue up per operation and the **last one is sticky**,
so a single mock serves any number of calls while a queue models state
transitions:

<!-- name: testing_walkthrough; fixtures: executing, success, operationId as id -->
```js
client.mock("getOperationStatus", executing);
client.mock("getOperationStatus", success);

await client.getOperationStatus(id); // -> executing
await client.getOperationStatus(id); // -> success
await client.getOperationStatus(id); // -> success (sticky)
```

Pass `{ error }` to make an operation throw, and arrays for iterator
operations:

<!-- name: testing_walkthrough; fixtures: initEvent, exitEvent -->
```js
import { NotFoundError } from "contree-client";

client.mock("getFile", null, { error: new NotFoundError(404, "nope") });
client.mock("iterOperationEvents", [initEvent, exitEvent]);
```

## Guard rails

- an unmocked operation rejects loudly (`no mock configured for ...`)
  instead of silently resolving `undefined`;
- `mock()` rejects unknown operation names, so a typo fails the test
  rather than arming a mock nothing consumes;
- `constructedWith` records the constructor arguments — convenient for
  testing factories and `fromProfile()`-style code paths.

The double is just another client: code annotated against
`ContreeClient` (or duck-typed) cannot tell the difference, exactly
like the [Python testing double](../python/testing.md).

## Live integration tests

Run the JavaScript live scenarios from the repository root:

```sh
CONTREE_TOKEN=... CONTREE_URL=... make test-live-js
```

Set `CONTREE_SPEC` to generate the client and `CONTREE_PROJECT` when the endpoint
requires a project header. These tests start disposable instances and exercise
subprocess creation, stdin, and cancellation. Use a test endpoint and token.

Pytest runs the scenarios in `client-js/test/test_js_integration.py`. The
`jsrun(source)` fixture writes an ES module into `tmp_path` and executes it with
Node.js. The scenarios import this checkout's generated client; CI installs the
built npm artifact at that location first. Credentials pass through the
environment, and failures and skip reasons appear in the pytest report.

`make test-live` runs the Python, JavaScript, and Go scenarios in one pytest
session. GitHub Actions runs each language in a separate pytest step within
the shared integration job, using the same repository secrets.
