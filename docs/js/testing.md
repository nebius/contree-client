# Testing

`contree-client/testing` ships an offline double with the exact
generated client surface: every API method exists, records its calls
and replays outcomes you queue — no network, no mocks of your own.

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

```js
client.mock("getOperationStatus", executing);
client.mock("getOperationStatus", success);

await client.getOperationStatus(id); // -> executing
await client.getOperationStatus(id); // -> success
await client.getOperationStatus(id); // -> success (sticky)
```

Pass `{ error }` to make an operation throw, and arrays for iterator
operations:

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
