# contree-client

Python and JavaScript clients for the Contree API, generated from one
OpenAPI specification at build time. Same wire protocol, same models,
same helpers — pick the language, the concepts transfer one-to-one. The
[Tutorial](tutorial.md) walks through the whole API across languages,
side by side.

- **Python** — sync and async flavours over pluggable
  [transport adapters](python/adapters.md) (stdlib http.client,
  urllib3, requests, httpx, aiohttp); the whole API surface lives on
  the `ContreeSyncClient` / `ContreeAsyncClient` base classes, so code
  written against them works with any backend.
- **JavaScript** — pure ESM with TypeScript declarations over the
  platform `fetch` (Node ≥ 18.17 and browsers); camelCase methods,
  wire-spelled fields.

## Install

```shell
pip install contree-client            # stdlib http.client backend only
pip install contree-client[urllib3]   # + urllib3
pip install contree-client[requests]  # + requests
pip install contree-client[httpx]     # + httpx (sync and async)
pip install contree-client[aiohttp]   # + aiohttp (async)

npm install contree-client            # JavaScript/TypeScript (fetch, ESM)
```

## Quick start

::::{tab-set}

:::{tab-item} Python · sync
:sync: sync

```python
from contree_client.sync import ContreeClient  # first installed backend

with ContreeClient.from_profile() as client:   # or ContreeClient("IAM_TOKEN", project=...)
    response = client.spawn_instance("uname -a", "tag:ubuntu:latest", shell=True)
    for event in client.iter_operation_events(response.uuid, follow=True):
        print(event.type, event.data)
```
:::

:::{tab-item} Python · async
:sync: async

```python
from contree_client.asyncio import ContreeAsyncClient  # first installed backend

async with ContreeAsyncClient.from_profile() as client:  # or ContreeAsyncClient("IAM_TOKEN", project=...)
    response = await client.spawn_instance("uname -a", "tag:ubuntu:latest", shell=True)
    async for event in client.iter_operation_events(response.uuid, follow=True):
        print(event.type, event.data)
```
:::

:::{tab-item} JavaScript
:sync: js

```js
import { ContreeClient } from "contree-client";

const client = await ContreeClient.fromProfile(); // or new ContreeClient("IAM_TOKEN", { project })
const response = await client.spawnInstance("uname -a", "tag:ubuntu:latest", {
  shell: true,
});
for await (const event of client.iterOperationEvents(response.uuid, {
  follow: true,
})) {
  console.log(event.type, event.data);
}
await client.close();
```
:::

::::

## Documentation

```{toctree}
:maxdepth: 1

tutorial
```

```{toctree}
:caption: Python client
:maxdepth: 2

python/adapters
python/api
python/testing
```

```{toctree}
:caption: JavaScript client
:maxdepth: 2

js/api
js/reference
js/testing
```

## License

Both packages are distributed under the Apache License, Version 2.0.

Copyright Nebius B.V. 2026 (see the "LICENSE" file in the repository).
