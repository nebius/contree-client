# Transport adapters

Every backend implements the same interface: synchronous clients
subclass {class}`~contree_client.base.ContreeSyncClient`, asynchronous
ones subclass {class}`~contree_client.base.ContreeAsyncClient`. An
adapter provides only the transport primitives — `request`,
`stream(spec, auto_decompress=True)`, `open` and `close` — the whole
API surface lives in the base classes and is documented on the
[generated API page](api.md).

Synchronous adapters export `ContreeClient`, asynchronous ones export
`ContreeAsyncClient`:

| Backend     | Extra                      | Import                                                          |
|-------------|----------------------------|-----------------------------------------------------------------|
| http.client | — (stdlib)                 | `from contree_client.http import ContreeClient`                 |
| urllib3     | `contree-client[urllib3]`  | `from contree_client.urllib3 import ContreeClient`              |
| requests    | `contree-client[requests]` | `from contree_client.requests import ContreeClient`             |
| httpx       | `contree-client[httpx]`    | `from contree_client.httpx import ContreeClient` / `ContreeAsyncClient` |
| aiohttp     | `contree-client[aiohttp]`  | `from contree_client.aiohttp import ContreeAsyncClient`         |

Thanks to the Liskov substitution principle any adapter of the
matching flavour is interchangeable — annotate code against the base
classes from {mod}`contree_client.types` and let the caller decide:

::::{tab-set}

:::{tab-item} Sync
:sync: sync

<!--
name: test_adapters_substitution
```python
from contree_client.models import ImageListResponse
from contree_client.testing import ContreeClient

IMAGE_UUID = "12345678-9abc-baba-deda-0123456789ab"
double = ContreeClient()
double.mock(
    "list_images",
    ImageListResponse.from_dict(
        {
            "images": [
                {"uuid": IMAGE_UUID, "created_at": "2024-01-01T12:00:00+00:00"}
            ]
        }
    ),
)
```
-->
```python
from contree_client.types import ContreeSyncClient


def biggest_image(client: ContreeSyncClient) -> str:
    images = client.list_images(tagged=True).images
    return max(images, key=lambda image: image.created_at or "").uuid


assert biggest_image(double) == IMAGE_UUID  # any backend, the test double too
```
:::

:::{tab-item} Async
:sync: async

<!--
name: async test_adapters_substitution_async
```python
from contree_client.models import ImageListResponse
from contree_client.testing import ContreeAsyncClient

IMAGE_UUID = "12345678-9abc-baba-deda-0123456789ab"
double = ContreeAsyncClient()
double.mock(
    "list_images",
    ImageListResponse.from_dict(
        {
            "images": [
                {"uuid": IMAGE_UUID, "created_at": "2024-01-01T12:00:00+00:00"}
            ]
        }
    ),
)
```
-->
```python
from contree_client.types import ContreeAsyncClient


async def biggest_image(client: ContreeAsyncClient) -> str:
    images = (await client.list_images(tagged=True)).images
    return max(images, key=lambda image: image.created_at or "").uuid


assert await biggest_image(double) == IMAGE_UUID
```
:::

::::

## Autodetect

When any installed backend will do, let the package pick one — the
first importable backend wins, ordered by ecosystem popularity (sync:
requests → urllib3 → httpx → stdlib http.client; async: aiohttp →
httpx). Popularity is judged by all-time PyPI download totals from
[ClickPy](https://clickpy.clickhouse.com) (PyPI analytics powered by
ClickHouse): requests ~34B, urllib3 ~39B (mostly transitive — it
ships inside requests/botocore), aiohttp ~10B, httpx ~7.6B. The async
variant raises an `ImportError` with an installation suggestion when
neither aiohttp nor httpx is installed:

<!-- name: test_adapters_autodetect -->
```python
from contree_client.sync import ContreeClient
from contree_client.asyncio import ContreeAsyncClient
```

## Creating a client

::::{tab-set}

:::{tab-item} Sync
:sync: sync

<!-- name: test_adapters_create -->
```python
from contree_client.requests import ContreeClient

client = ContreeClient(
    "IAM_TOKEN",                    # Authorization: Bearer <token>
    project="my-project-id",        # Project header (IAM); omit for JWT
    # base_url defaults to the public endpoint from the spec;
    # override for self-hosted deployments:
    # base_url="https://contree.example.com",
    timeout=300.0,
)
```
:::

:::{tab-item} Async
:sync: async

<!-- name: test_adapters_create_async -->
```python
from contree_client.httpx import ContreeAsyncClient

client = ContreeAsyncClient(
    "IAM_TOKEN",                    # Authorization: Bearer <token>
    project="my-project-id",        # Project header (IAM); omit for JWT
    # base_url defaults to the public endpoint from the spec;
    # override for self-hosted deployments:
    # base_url="https://contree.example.com",
    timeout=300.0,
)
```
:::

::::

All clients are context managers. Entering the context calls
`open()` — asynchronous adapters use it to create loop-bound
resources (the `aiohttp.ClientSession`) eagerly; `close()` releases
the transport:

::::{tab-set}

:::{tab-item} Sync
:sync: sync

<!--
name: test_adapters_context_manager
```python
from contree_client import testing
from contree_client.models import WhoAmIResponse

WHOAMI = WhoAmIResponse.from_dict(
    {
        "token_uuid": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
        "token_expiration": None,
        "permissions": {"spawn": True},
        "operations_stat": {},
    }
)


class ContreeClient(testing.ContreeClient):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.mock("whoami", WHOAMI)
```
-->
```python
with ContreeClient("IAM_TOKEN", project="p") as client:
    print(client.whoami().permissions)
```
:::

:::{tab-item} Async
:sync: async

<!--
name: async test_adapters_context_manager_async
```python
from contree_client import testing
from contree_client.models import WhoAmIResponse

WHOAMI = WhoAmIResponse.from_dict(
    {
        "token_uuid": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
        "token_expiration": None,
        "permissions": {"spawn": True},
        "operations_stat": {},
    }
)


class ContreeAsyncClient(testing.ContreeAsyncClient):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.mock("whoami", WHOAMI)
```
-->
```python
async with ContreeAsyncClient("IAM_TOKEN", project="p") as client:
    print((await client.whoami()).permissions)
```
:::

::::

### Reuse a saved profile

With a Contree profile saved under `$CONTREE_HOME` (the configuration
shared by all Contree tooling), every adapter can be built from the
same profiles (`$CONTREE_HOME/auth.ini`):

::::{tab-set}

:::{tab-item} Sync
:sync: sync

<!--
name: test_adapters_from_profile;
fixtures: tmp_path, monkeypatch
```python
from contree_client.testing import ContreeClient

(tmp_path / "auth.ini").write_text(
    "[DEFAULT]\n"
    "profile = default\n"
    "[profile:default]\n"
    "token = SECRET\n"
    "url = https://contree.example.com\n"
    "[profile:staging]\n"
    "token = STAGING-SECRET\n"
    "url = https://staging.example.com\n"
)
monkeypatch.setenv("CONTREE_HOME", str(tmp_path))
monkeypatch.delenv("CONTREE_PROFILE", raising=False)
```
-->
```python
client = ContreeClient.from_profile()           # the active profile
client = ContreeClient.from_profile("staging")  # a specific one
```
:::

:::{tab-item} Async
:sync: async

<!--
name: test_adapters_from_profile_async;
fixtures: tmp_path, monkeypatch
```python
from contree_client.testing import ContreeAsyncClient

(tmp_path / "auth.ini").write_text(
    "[DEFAULT]\n"
    "profile = default\n"
    "[profile:default]\n"
    "token = SECRET\n"
    "url = https://contree.example.com\n"
    "[profile:staging]\n"
    "token = STAGING-SECRET\n"
    "url = https://staging.example.com\n"
)
monkeypatch.setenv("CONTREE_HOME", str(tmp_path))
monkeypatch.delenv("CONTREE_PROFILE", raising=False)
```
-->
```python
client = ContreeAsyncClient.from_profile()           # the active profile
client = ContreeAsyncClient.from_profile("staging")  # a specific one
```
:::

::::

Resolution order: explicit argument → `CONTREE_PROFILE` environment
variable → the active profile from the config file.

Environments without config files (CI, containers) can describe a
profile entirely through the standard Contree variables:
{func}`contree_client.profiles.from_env` returns a
{class}`~contree_client.profiles.Profile` built from `CONTREE_TOKEN`
(or `NEBIUS_API_KEY`), `CONTREE_URL` and optionally `CONTREE_PROJECT`
(or `NEBIUS_AI_PROJECT`) — or `None` when they are not set, so a
caller can fall through to `from_profile()`.

## Bring your own transport instance

Each adapter accepts a preconfigured transport object (proxies, TLS
tweaks, connection pooling — anything the library supports):

```python
from contree_client.urllib3 import ContreeClient as Urllib3Client
from contree_client.requests import ContreeClient as RequestsClient
from contree_client.httpx import ContreeAsyncClient as HttpxAsyncClient
from contree_client.aiohttp import ContreeAsyncClient as AiohttpClient

Urllib3Client("token", urllib3_pool_manager=urllib3.PoolManager(maxsize=50))
RequestsClient("token", requests_session=my_requests_session)
HttpxAsyncClient("token", httpx_client=httpx.AsyncClient(http2=True))
AiohttpClient("token", aiohttp_session=my_aiohttp_session)
```

Adapter-specific constructor arguments always carry the adapter name
as a prefix (`aiohttp_session`, `httpx_client`, ...) — everything
unprefixed is part of the common interface and works identically on
every backend.

A transport you pass in is *not* closed by `client.close()` — the
client only closes what it created itself.

## Custom TLS

Every adapter takes an optional `ssl_context` — a standard
`ssl.SSLContext` handed to the transport untouched (private CAs,
client certificates, pinned ciphers):

```python
import ssl

context = ssl.create_default_context(cafile="internal-ca.pem")
client = ContreeClient("token", ssl_context=context)
```

`ssl_context` cannot be combined with a bring-your-own transport
instance: the transport you built owns its TLS configuration, so
passing both raises `ValueError` instead of silently ignoring one.

## Connection reuse

Every adapter keeps pooled keepalive connections for the lifetime of
the client: urllib3 (`PoolManager`), requests (`Session`), httpx
(`Client` / `AsyncClient`), aiohttp (`ClientSession`), and the stdlib
`http.client` adapter runs its own small LIFO pool (the
`http_max_connections` constructor kwarg, 25 by default) — buffered
requests
borrow a warm connection instead of paying a TLS handshake per call,
concurrent callers borrow distinct connections (the cap bounds the
total; extra callers wait instead of stampeding the server). Before
handing a pooled connection out the pool verifies it is still alive
(an idle socket must be silent - EOF or unsolicited bytes mean the
server dropped it), so an expired keepalive is replaced by a fresh
dial without ever sending into a dead socket; a connection that dies
in the race window between the check and the send is still covered by
one transparent re-send. Streams always own a dedicated connection
until EOF.

## Compression

The server compresses every response (including streams) with gzip.
Adapters decode it transparently, incrementally for streams
(`Z_SYNC_FLUSH`-friendly), and every adapter advertises
`Accept-Encoding: gzip`; passing `auto_decompress=False` to
`stream()` yields the body exactly as served.

## Retries

Retries are opt-in: pass a `RetryPolicy` and every buffered request is
retried on the backend's transient network errors and on 410/425/5xx
responses, honoring `Retry-After` (delta-seconds or an HTTP-date) when
the server sends one and walking a backoff ladder (0.1 → 5 s)
otherwise. The budget is finite by default (10 attempts); unbounded
retries are an explicit `max_attempts=None`. Non-idempotent requests
(POST — spawn, import, upload) are **never replayed** unless you opt
into the double-execution risk with `retry_unsafe=True`: a lost
response may mean the server already executed the call. File-like
request bodies are rewound to their initial offset before each attempt
(non-seekable bodies fail fast). Buffered-request timeouts follow the
same retry policy. The configured `timeout` applies to each attempt, so
the complete call can take longer. An explicit `wait_operation` or
`follow_operation_events` timeout is shared by event connections, status
probes, polling, retry delays, and the final status fetch. Sync buffered reads
can report expiry after the response completes. Status probes use one transport
attempt because the event follower controls reconnection. `RetryPolicy` never
retries streaming requests.
`follow_operation_events` reconnects transient stream failures, including
transport timeouts, with `Last-Event-Id`. A terminal or cancelled operation
stops reconnection.

::::{tab-set}

:::{tab-item} Sync
:sync: sync

<!--
name: test_adapters_retry
```python
from contree_client.requests import ContreeClient
```
-->
```python
from contree_client import RetryPolicy

client = ContreeClient(
    "IAM_TOKEN",
    retry=RetryPolicy(),                     # finite budget: 10 attempts
    # retry=RetryPolicy(max_attempts=None),  # explicitly unbounded
    # retry=RetryPolicy(retry_unsafe=True),  # replay POSTs too (risky)
)
```
:::

:::{tab-item} Async
:sync: async

<!--
name: test_adapters_retry_async
```python
from contree_client.httpx import ContreeAsyncClient
```
-->
```python
from contree_client import RetryPolicy

client = ContreeAsyncClient(
    "IAM_TOKEN",
    retry=RetryPolicy(),                     # finite budget: 10 attempts
    # retry=RetryPolicy(max_attempts=None),  # explicitly unbounded
    # retry=RetryPolicy(retry_unsafe=True),  # replay POSTs too (risky)
)
```
:::

::::

## Transport errors

Each adapter maps the failure categories exposed by its backend into
the shared [error hierarchy](api.md#transport-errors). Not every
backend provides a distinct native type for every category. Adapter
wrappers also inherit the corresponding native backend base, so
existing native exception handlers keep working.

The wrapper preserves useful native diagnostic details and exposes the
native exception through `error.original`. It replaces tuple-only
diagnostics with descriptive text. Empty backend timeout messages
become `Request timed out`.

Some specialized wrappers also match the adapter's older
`Contree*ConnectionError` class for compatibility. When one handler
catches multiple Contree categories, put TLS, closed-connection, and
protocol handlers before the general connection handler.

One gotcha: requests classifies a stalled *read* on an already-open
stream as its own `ConnectionError`, not `Timeout` - that is requests'
own choice, not a contree-client one - so a stalled `stream()`
download on this backend raises `ContreeConnectionError`, not
`ContreeTimeoutError`.

## User-Agent

Every request carries `contree-client/<version> <transport>/<version>
Python/<version> <platform>` unless the caller supplies its own
`User-Agent` header. The tokens come from the `UA_*` class attributes
of the base client; adapters override `UA_TRANSPORT_LIBRARY` (e.g.
`httpx/0.28.1`, `http.client` for the stdlib backend).

An application announces itself with the `identity` constructor
kwarg — its token leads and the library tokens stay intact:

::::{tab-set}

:::{tab-item} Sync
:sync: sync

<!--
name: test_adapters_identity
```python
from contree_client.httpx import ContreeClient
```
-->
```python
client = ContreeClient("IAM_TOKEN", identity="my-app/1.2.3")
# User-Agent: my-app/1.2.3 contree-client/0.1.0 httpx/0.28.1 Python/...

assert client.user_agent().startswith("my-app/1.2.3 contree-client/")
```
:::

:::{tab-item} Async
:sync: async

<!--
name: test_adapters_identity_async
```python
from contree_client.httpx import ContreeAsyncClient
```
-->
```python
client = ContreeAsyncClient("IAM_TOKEN", identity="my-app/1.2.3")
# User-Agent: my-app/1.2.3 contree-client/0.1.0 httpx/0.28.1 Python/...

assert client.user_agent().startswith("my-app/1.2.3 contree-client/")
```
:::

::::

## Logging

The library logs under the `contree_client` logger with one child per
adapter (`contree_client.requests`, `contree_client.aiohttp`, ...).
The base logger level is explicitly set to `logging.ERROR`, so even an
application configured with root `DEBUG` sees nothing from the client
unless it opts in:

<!-- name: test_adapters_logging -->
```python
import logging

from contree_client.types import set_log_level

set_log_level(logging.DEBUG)
```
<!--
name: test_adapters_logging
```python
set_log_level(logging.ERROR)  # keep the other examples quiet
```
-->

At `DEBUG` the client logs the raw exchange:

- outgoing requests — method, URL, headers and body (`Authorization`
  and other sensitive headers are redacted; JSON bodies are logged
  with the values of secret-suffixed keys structurally redacted,
  truncated at 4 KiB; binary bodies become a `<binary NB>` marker);
- buffered responses — status, headers and body, same formatting;
- streaming responses — the status line and per-chunk sizes;
- every received SSE event (`sse event: OperationEvent(...)`);
- backend autodetection decisions.

## Reference

### contree_client.types

```{eval-rst}
.. automodule:: contree_client.types
```

### contree_client.sync

```{eval-rst}
.. automodule:: contree_client.sync
```

### contree_client.asyncio

```{eval-rst}
.. automodule:: contree_client.asyncio
```

### contree_client.http

```{eval-rst}
.. automodule:: contree_client.http
```

### contree_client.urllib3

```{eval-rst}
.. automodule:: contree_client.urllib3
```

### contree_client.requests

```{eval-rst}
.. automodule:: contree_client.requests
```

### contree_client.httpx

```{eval-rst}
.. automodule:: contree_client.httpx
```

### contree_client.aiohttp

```{eval-rst}
.. automodule:: contree_client.aiohttp
```

### contree_client.profiles

```{eval-rst}
.. automodule:: contree_client.profiles
```

### contree_client.runtime

```{eval-rst}
.. automodule:: contree_client.runtime
```
