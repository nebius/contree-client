# contree-client

Python client for the Contree API — sandboxed code execution
combining virtual machine isolation with container operational
efficiency.

Synchronous clients are named `ContreeClient`, asynchronous ones are
named `ContreeAsyncClient`; every backend shares the same interface:

| Backend     | Extra                      | Import                                                          |
|-------------|----------------------------|-----------------------------------------------------------------|
| http.client | — (stdlib)                 | `from contree_client.http import ContreeClient`                 |
| urllib3     | `contree-client[urllib3]`  | `from contree_client.urllib3 import ContreeClient`              |
| requests    | `contree-client[requests]` | `from contree_client.requests import ContreeClient`             |
| httpx       | `contree-client[httpx]`    | `from contree_client.httpx import ContreeClient` / `ContreeAsyncClient` |
| aiohttp     | `contree-client[aiohttp]`  | `from contree_client.aiohttp import ContreeAsyncClient`         |

Or let the package pick the first installed backend:

<!-- name: test_readme_autodetect -->
```python
from contree_client.sync import ContreeClient
from contree_client.asyncio import ContreeAsyncClient
```

## Quick start

<!--
name: test_readme_quick_start;
fixtures: tmp_path, monkeypatch
```python
from contree_client import models, testing

OP_UUID = "87654321-9abc-baba-deda-0123456789ab"
EVENTS = [
    models.OperationEvent.from_dict(event)
    for event in (
        {
            "id": 1,
            "ts": "2026-06-08T20:00:00Z",
            "spid": 1,
            "type": "stdout",
            "data": {"value": "Linux\n", "encoding": "ascii"},
        },
        {
            "id": 2,
            "ts": "2026-06-08T20:00:01Z",
            "spid": 0,
            "type": "completion",
            "data": {"status": "SUCCESS", "error": None, "duration_ms": 12},
        },
    )
]


class QuickStartDouble(testing.ContreeClient):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.mock(
            "spawn_instance",
            models.InstanceSpawnResponse.from_dict({"uuid": OP_UUID}),
        )
        self.mock("iter_operation_events", EVENTS)


(tmp_path / "auth.ini").write_text(
    "[DEFAULT]\nprofile = default\n"
    "[profile:default]\ntoken = SECRET\nurl = https://contree.example.com\n"
)
monkeypatch.setenv("CONTREE_HOME", str(tmp_path))
monkeypatch.delenv("CONTREE_PROFILE", raising=False)
monkeypatch.setattr("contree_client.sync.ContreeClient", QuickStartDouble)
```
-->
```python
from contree_client.sync import ContreeClient

# reuse a saved Contree profile,
# or pass credentials directly: ContreeClient("IAM_TOKEN", project=...)
with ContreeClient.from_profile() as client:
    response = client.spawn_instance("uname -a", "tag:ubuntu:latest", shell=True)
    for event in client.iter_operation_events(response.uuid, follow=True):
        print(event.type, event.data)
```

## Use cases

Run a command and read its output:

<!--
name: test_readme_use_cases;
fixtures: tmp_path, monkeypatch
```python
import gzip
import io
import tarfile

from contree_client import models, testing

OP_UUID = "87654321-9abc-baba-deda-0123456789ab"
IMG_UUID = "12345678-9abc-baba-deda-0123456789ab"
EVENTS = [
    models.OperationEvent.from_dict(event)
    for event in (
        {
            "id": 1,
            "ts": "2026-06-08T20:00:00Z",
            "spid": 1,
            "type": "stdout",
            "data": {"value": "hello\n", "encoding": "ascii"},
        },
        {
            "id": 2,
            "ts": "2026-06-08T20:00:01Z",
            "spid": 0,
            "type": "completion",
            "data": {"status": "SUCCESS", "error": None, "duration_ms": 12},
        },
    )
]
OPERATION = models.OperationResponse.from_dict(
    {
        "uuid": OP_UUID,
        "kind": "instance",
        "status": "SUCCESS",
        "metadata": {
            "command": "echo hello",
            "image": "tag:ubuntu:latest",
            "result": {"stdout": {"value": "hello\n", "encoding": "ascii"}},
        },
    }
)
buffer = io.BytesIO()
with tarfile.open(fileobj=buffer, mode="w") as tar:
    payload = b"127.0.0.1 localhost\n"
    info = tarfile.TarInfo("etc/hosts")
    info.size = len(payload)
    tar.addfile(info, io.BytesIO(payload))
TAR_BYTES = buffer.getvalue()


class UseCaseDouble(testing.ContreeClient):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.mock(
            "spawn_instance",
            models.InstanceSpawnResponse.from_dict({"uuid": OP_UUID}),
        )
        self.mock("get_operation_status", OPERATION)
        self.mock("iter_operation_events", EVENTS)
        self.mock(
            "upload_file",
            models.FileResponse.from_dict(
                {
                    "uuid": "a9165a5d-5c86-4bd8-8ee4-ae46c19cf45d",
                    "sha256": "0" * 64,
                    "size": 8,
                }
            ),
        )
        self.mock("import_image", OP_UUID)
        self.mock("inspect_find_image_by_tag", IMG_UUID)
        self.mock(
            "inspect_image_list",
            models.DirectoryList.from_dict({"path": "/etc", "files": []}),
        )
        self.mock("inspect_image_archive", [TAR_BYTES])


monkeypatch.chdir(tmp_path)
(tmp_path / "data.txt").write_text("one\ntwo\n")
(tmp_path / "auth.ini").write_text(
    "[DEFAULT]\nprofile = default\n"
    "[profile:default]\ntoken = SECRET\nurl = https://contree.example.com\n"
)
monkeypatch.setenv("CONTREE_HOME", str(tmp_path))
monkeypatch.delenv("CONTREE_PROFILE", raising=False)
ContreeClient = UseCaseDouble
client = UseCaseDouble("IAM_TOKEN")
```
-->
```python
from contree_client import OperationStatus

response = client.spawn_instance(
    "echo hello",
    "tag:ubuntu:latest",
    shell=True,
    disposable=True,   # do not persist a result image
    timeout=60,
)
operation = client.wait_operation(response.uuid)
assert operation.status is OperationStatus.SUCCESS
print(operation.metadata.result.stdout.as_text())
```

Follow the live event stream with transparent reconnection
(`Last-Event-Id` resume; ends on the `completion` event):

<!-- name: test_readme_use_cases -->
```python
import sys

from contree_client import decode_chunk

for event in client.follow_operation_events(response.uuid):
    if event.type in ("stdout", "stderr"):
        sys.stdout.buffer.write(decode_chunk(event.data))
```

Retry transient failures (network errors, 410/425/5xx) automatically:

<!-- name: test_readme_use_cases -->
```python
from contree_client import RetryPolicy

client = ContreeClient.from_profile(retry=RetryPolicy())
```

Stage files into the sandbox:

<!-- name: test_readme_use_cases -->
```python
from contree_client import FileSpec

uploaded = client.upload_file(open("data.txt", "rb"))
client.spawn_instance(
    "wc -l /work/data.txt",
    "tag:ubuntu:latest",
    shell=True,
    files={"/work/data.txt": FileSpec(uuid=uploaded.uuid, mode="0644")},
)
```

Import an image and inspect it without spawning anything:

<!-- name: test_readme_use_cases -->
```python
from contree_client import ImageImportRegistry

operation_id = client.import_image(
    ImageImportRegistry(url="docker://docker.io/library/busybox:latest"),
    tag="busybox:latest",
)
image_uuid = client.inspect_find_image_by_tag("busybox:latest")
print(client.inspect_image_list(image_uuid, "/etc").files)
```

Download a directory as a tar stream:

<!-- name: test_readme_use_cases -->
```python
with open("etc.tar", "wb") as archive:
    for chunk in client.inspect_image_archive(image_uuid, "/etc"):
        archive.write(chunk)
```

Asynchronous flavour mirrors the sync API exactly:

<!--
name: async test_readme_async;
fixtures: tmp_path, monkeypatch
```python
from contree_client import models, testing


class AsyncDouble(testing.ContreeAsyncClient):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.mock(
            "whoami",
            models.WhoAmIResponse.from_dict(
                {
                    "token_uuid": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
                    "token_expiration": None,
                    "permissions": {"spawn": True},
                    "operations_stat": {},
                }
            ),
        )


(tmp_path / "auth.ini").write_text(
    "[DEFAULT]\nprofile = default\n"
    "[profile:default]\ntoken = SECRET\nurl = https://contree.example.com\n"
)
monkeypatch.setenv("CONTREE_HOME", str(tmp_path))
monkeypatch.delenv("CONTREE_PROFILE", raising=False)
monkeypatch.setattr("contree_client.asyncio.ContreeAsyncClient", AsyncDouble)
```
-->
```python
from contree_client.asyncio import ContreeAsyncClient

async with ContreeAsyncClient.from_profile() as client:
    me = await client.whoami()
    print(me.permissions)
```

Test your code without a server — the in-memory double from
`contree_client.testing` implements the same interface as every real
backend; unmocked methods raise, calls are recorded:

<!--
name: test_readme_testing
```python
from contree_client import models

spawn_response = models.InstanceSpawnResponse.from_dict(
    {"uuid": "87654321-9abc-baba-deda-0123456789ab"}
)
stdout_event = models.OperationEvent.from_dict(
    {
        "id": 1,
        "ts": "2026-06-08T20:00:00Z",
        "spid": 1,
        "type": "stdout",
        "data": {"value": "hi\n", "encoding": "ascii"},
    }
)
exit_event = stdout_event


def run_my_code(client):
    response = client.spawn_instance("uname -a", "tag:ubuntu:latest", shell=True)
    for event in client.iter_operation_events(response.uuid):
        pass
```
-->
```python
from contree_client.testing import ContreeClient

client = ContreeClient()
client.mock("spawn_instance", spawn_response)
client.mock("iter_operation_events", [stdout_event, exit_event])

run_my_code(client)

assert client.calls_for("spawn_instance")
```

Debug logging (redacted, off by default):

<!-- name: test_readme_logging -->
```python
import logging

from contree_client.types import set_log_level

set_log_level(logging.DEBUG)
```
<!--
name: test_readme_logging
```python
set_log_level(logging.ERROR)  # keep the other examples quiet
```
-->
