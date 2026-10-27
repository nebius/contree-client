"""Live Go client scenarios, executed by pytest through gorun."""

# Go snippets use Go formatting rather than Python's line-length limit.
# ruff: noqa: E501

from __future__ import annotations

import json
import os
from collections.abc import Callable

import pytest

pytestmark = pytest.mark.integration

COMMON = r"""
package main

import (
    "context"
    "encoding/json"
    "fmt"
    "os"
    "strings"
    "time"
    EXTRA_IMPORTS

    contree "github.com/nebius/contree-client/go/v1"
)

func must[T any](value T, err error) T {
    if err != nil {
        panic(err)
    }

    return value
}

func check(ok bool, message string) {
    if !ok {
        panic(message)
    }
}

func noError(err error) {
    if err != nil {
        panic(err)
    }
}

func image(ctx context.Context, c *contree.Client) string {
    check(SAMPLE_IMAGE_UUID != "", "the session image fixture is required")
    return SAMPLE_IMAGE_UUID
}

func spawn(ctx context.Context, c *contree.Client, command, image string, options contree.SpawnInstanceOptions) string {
    response := must(c.SpawnInstance(ctx, command, image, options))
    id, present := response.UUID.Value()
    check(present && id != "", "spawn response has no operation UUID")

    return id
}

func cleanup(c *contree.Client, id string) {
    ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
    defer cancel()

    operation := must(c.GetOperationStatus(ctx, id, &contree.GetOperationStatusOptions{Inflight: true}))
    status, _ := operation.Status.Value()
    if status.IsTerminal() {
        return
    }

    noError(c.CancelOperation(ctx, id))

    follow := false

    terminal := must(c.WaitOperation(ctx, id, contree.WaitOperationOptions{FollowEvents: &follow}))
    status, _ = terminal.Status.Value()
    check(status.IsTerminal(), "cleanup did not stop the operation")
}

func success(operation contree.OperationResponse) {
    status, _ := operation.Status.Value()
    detail, _ := operation.Error.Value()
    check(string(status) == "SUCCESS", fmt.Sprintf("operation ended with %s: %s", status, detail))
}

func stdout(operation contree.OperationResponse) string {
    metadata, _ := operation.Metadata.Value()
    var result contree.InstanceResult
    switch value := metadata.(type) {
    case *contree.OperationInstanceMetadata:
        result, _ = value.Result.Value()
    case contree.OperationInstanceMetadata:
        result, _ = value.Result.Value()
    default:
        panic(fmt.Sprintf("unexpected metadata type %T", metadata))
    }

    stream, _ := result.Stdout.Value()
    return contree.DecodeStream(stream)
}

func main() {
    ctx, cancel := context.WithTimeout(context.Background(), 180*time.Second)
    defer cancel()

    profile, ok := contree.ProfileFromEnvironment()
    check(ok, "CONTREE_TOKEN and CONTREE_URL are required")

    c := must(contree.NewClientFromProfile(profile, contree.WithRetry(contree.DefaultRetryPolicy())))

    reason := run(ctx, c)

    noError(json.NewEncoder(os.Stdout).Encode(map[string]string{"skip": reason}))
}

func run(ctx context.Context, c *contree.Client) string {
    SCENARIO

    return ""
}
"""


@pytest.fixture
def live_go(gorun: Callable[[str], str]) -> Callable[..., None]:
    if not (os.environ.get("CONTREE_TOKEN") or os.environ.get("NEBIUS_API_KEY")):
        pytest.skip("Go live tests require CONTREE_TOKEN or NEBIUS_API_KEY")
    if not os.environ.get("CONTREE_URL"):
        pytest.fail("Go live tests require an explicit CONTREE_URL")

    def run(source: str, *, imports: str = "", image_uuid: str = "") -> None:
        program = (
            COMMON.replace("EXTRA_IMPORTS", imports)
            .replace("SCENARIO", source)
            .replace("SAMPLE_IMAGE_UUID", json.dumps(image_uuid))
        )
        result = json.loads(gorun(program))
        if result["skip"]:
            pytest.skip(result["skip"])

    return run


@pytest.fixture
def live_go_image(
    live_go: Callable[..., None], live_image_uuid: str
) -> Callable[..., None]:
    def run(source: str, *, imports: str = "") -> None:
        live_go(source, imports=imports, image_uuid=live_image_uuid)

    return run


def test_go_auth_and_lists(live_go: Callable[..., None]) -> None:
    live_go(r"""
    me := must(c.WhoAmI(ctx))
    check(me.TokenUUID != "" && len(me.Permissions) != 0, "empty token introspection")

    limit := int64(5)
    response := must(c.ListImages(ctx, &contree.ListImagesOptions{Limit: &limit}))

    images, _ := response.Images.Value()

    for _, item := range images {
        id, _ := item.UUID.Value()
        check(id != "", "image has no UUID")
    }

    for _, item := range must(c.ListOperations(ctx, &contree.ListOperationsOptions{Limit: &limit})) {
        id, _ := item.UUID.Value()
        check(id != "", "operation has no UUID")
    }
    """)


def test_go_upload_and_dedup(live_go: Callable[..., None]) -> None:
    live_go(
        r"""
    payload := fmt.Sprintf("contree-go-integration-%d\n", time.Now().UnixNano())
    digest := fmt.Sprintf("%x", sha256.Sum256([]byte(payload)))
    check(!must(c.CheckFileExists(ctx, digest)), "fresh content already exists")

    uploaded := must(c.UploadFile(ctx, strings.NewReader(payload)))
    check(uploaded.SHA256 == digest && uploaded.Size == int64(len(payload)), "upload metadata mismatch")
    check(must(c.CheckFileExists(ctx, digest)), "uploaded file does not exist")

    file := must(c.GetFile(ctx, digest))
    check(file.UUID == uploaded.UUID && file.SHA256 == digest, "stored file metadata mismatch")

    existing := must(c.EnsureFile(ctx, strings.NewReader(payload), nil))
    check(existing.UUID == uploaded.UUID, "EnsureFile did not reuse the uploaded file")
    """,
        imports='"crypto/sha256"',
    )


def test_go_inspect_download_and_archive(live_go_image: Callable[..., None]) -> None:
    live_go_image(
        r"""
    id := image(ctx, c)
    if id == "" {
        return "no runnable tagged image"
    }

    inspected := must(c.InspectImage(ctx, id))
    actualID, _ := inspected.UUID.Value()
    check(actualID == id, "inspect returned a different image")
    check(len(must(c.InspectImageList(ctx, id, "/")).Files) > 0, "root directory is empty")

    path := "/etc/passwd"
    if !must(c.CheckImageFile(ctx, id, path)) {
        return "sample image has no /etc/passwd"
    }
    check(!must(c.CheckImageFile(ctx, id, "/contree-missing-"+fmt.Sprint(time.Now().UnixNano()))), "missing file exists")

    content := must(c.InspectImageDownload(ctx, id, path))
    stream := must(c.InspectImageDownloadStream(ctx, id, path))
    streamed, err := io.ReadAll(stream)
    noError(stream.Close())
    noError(err)
    check(len(content) > 0 && bytes.Equal(content, streamed), "buffered and streamed downloads differ")

    paths := []string{path}
    matches := must(c.InspectImageGrep(ctx, id, []string{"root"}, &contree.InspectImageGrepOptions{Path: &paths}))
    check(len(matches.Matches) > 0, "grep did not find root")

    check(must(c.CheckImageArchive(ctx, id, "/etc")), "/etc cannot be archived")
    archive := must(c.InspectImageArchive(ctx, id, "/etc"))
    defer archive.Close()

    tarReader := tar.NewReader(archive)
    entries := 0
    for {
        _, err := tarReader.Next()
        if err == io.EOF {
            break
        }
        noError(err)
        entries++
    }

    check(entries > 0, "archive contains no entries")
    """,
        imports='"archive/tar"; "bytes"; "io"',
    )


SPAWN_SETUP = r"""
    permissions := must(c.WhoAmI(ctx)).Permissions
    if !permissions["cancel"] || !(permissions["spawn"] || permissions["spawn_disposable"]) {
        return "token lacks spawn or cancel permission"
    }

    imageID := image(ctx, c)
    if imageID == "" {
        return "no runnable tagged image"
    }
"""


def test_go_spawn_follow_wait_and_replay(live_go_image: Callable[..., None]) -> None:
    live_go_image(
        SPAWN_SETUP
        + r"""

    marker := fmt.Sprintf("contree-go-%d", time.Now().UnixNano())
    id := spawn(ctx, c, "echo "+marker, imageID, contree.SpawnInstanceOptions{
        Shell: true, Disposable: true, Timeout: 30*time.Second,
    })
    defer cleanup(c, id)

    var output strings.Builder
    last := ""
    for event, err := range c.FollowOperationEvents(ctx, id, nil) {
        noError(err)
        last = string(event.Type)
        if last == "stdout" {
            output.Write(contree.DecodeChunk(event.Data))
        }
    }
    check(last == "completion" && strings.Contains(output.String(), marker), "live SSE lost output or completion")

    operation := must(c.WaitOperation(ctx, id))
    success(operation)
    check(strings.Contains(stdout(operation), marker), "operation metadata lost stdout")

    output.Reset()
    last = ""
    for event, err := range c.IterOperationEvents(ctx, id, nil) {
        noError(err)
        last = string(event.Type)
        if last == "stdout" {
            output.Write(contree.DecodeChunk(event.Data))
        }
    }
    check(last == "completion" && strings.Contains(output.String(), marker), "retained SSE lost output or completion")
    """
    )


def test_go_cancel(live_go_image: Callable[..., None]) -> None:
    live_go_image(
        SPAWN_SETUP
        + r"""
    id := spawn(ctx, c, "sleep 120", imageID, contree.SpawnInstanceOptions{
        Shell: true, Disposable: true, Timeout: 150*time.Second,
    })
    defer cleanup(c, id)

    noError(c.CancelOperation(ctx, id))

    follow := false

    operation := must(c.WaitOperation(ctx, id, contree.WaitOperationOptions{FollowEvents: &follow}))
    status, _ := operation.Status.Value()
    check(string(status) == "CANCELLED", fmt.Sprintf("cancel ended with %s", status))
    """
    )


def test_go_preserve_cwd(live_go_image: Callable[..., None]) -> None:
    live_go_image(
        SPAWN_SETUP
        + r"""
    if !permissions["spawn"] {
        return "token lacks persistent spawn permission"
    }

    id := spawn(ctx, c, "pwd", imageID, contree.SpawnInstanceOptions{
        Shell: true, Cwd: "/tmp", PreserveCwd: true, Timeout: 30*time.Second,
    })
    defer cleanup(c, id)

    operation := must(c.WaitOperation(ctx, id))
    success(operation)
    check(strings.TrimSpace(stdout(operation)) == "/tmp", "initial cwd was not /tmp")

    resultImage, _ := operation.ResultImageUUID.Value()
    if resultImage == "" {
        result, _ := operation.Result.Value()
        resultImage, _ = result.Image.Value()
    }
    check(resultImage != "", "persistent operation returned no image")

    second := spawn(ctx, c, "pwd", resultImage, contree.SpawnInstanceOptions{
        Shell: true, Disposable: true, Timeout: 30*time.Second,
    })
    defer cleanup(c, second)

    result := must(c.WaitOperation(ctx, second))
    success(result)
    check(strings.TrimSpace(stdout(result)) == "/tmp", "result image did not preserve cwd")
    """
    )


def test_go_live_instance_files_and_events(live_go_image: Callable[..., None]) -> None:
    live_go_image(
        SPAWN_SETUP
        + r"""

    if !permissions["spawn_disposable"] {
        return "token lacks disposable spawn permission"
    }

    marker := fmt.Sprintf("ready-%d", time.Now().UnixNano())
    payload := []byte(fmt.Sprintf("live-file-%d\n", time.Now().UnixNano()))
    id := spawn(ctx, c, "echo "+marker+"; read line", imageID, contree.SpawnInstanceOptions{
        Shell: true, Disposable: true, Timeout: 180 * time.Second,
        Stdin:           &contree.ClosableStreamRepr{Value: "", Close: contree.Some(false)},
        ResourcesLimits: &contree.InstanceResourcesLimits{CPU: contree.Some(int64(2)), MemoryMib: contree.Some(int64(256))},
    })
    defer cleanup(c, id)

    ready := false
    var output strings.Builder
    for event, err := range c.FollowOperationEvents(ctx, id, nil) {
        noError(err)
        if event.Type == contree.OperationEventTypeStdout {
            output.Write(contree.DecodeChunk(event.Data))
            if strings.Contains(output.String(), marker) {
                ready = true
                break
            }
        }
    }
    check(ready, "instance exited before readiness")

    uploaded := must(c.UploadFile(ctx, bytes.NewReader(payload)))
    path := "/tmp/client-live/input.txt"
    noError(c.ApplyInstanceFile(ctx, id, uploaded.UUID, path, &contree.ApplyInstanceFileOptions{
        Unpack: contree.Some(false), Replace: contree.Some(false), Merge: contree.Some(false),
    }))
    check(must(c.CheckInstanceFile(ctx, id, path)), "uploaded file does not exist")
    check(!must(c.CheckInstanceFile(ctx, id, path+".missing")), "missing file exists")
    check(must(c.CheckInstanceArchive(ctx, id, "/tmp/client-live")), "directory cannot be archived")
    check(!must(c.CheckInstanceArchive(ctx, id, "/tmp/client-live-missing")), "missing archive exists")

    listing := must(c.InspectInstanceList(ctx, id, "/tmp/client-live"))
    found := false
    for _, file := range listing.Files {
        if strings.HasSuffix(file.Path, "input.txt") {
            found = true
        }
    }
    check(found, "list omitted uploaded file")
    check(bytes.Equal(must(c.InspectInstanceDownload(ctx, id, path)), payload), "download differs")

    stream := must(c.InspectInstanceDownloadStream(ctx, id, path))
    downloaded, err := io.ReadAll(stream)
    noError(stream.Close())
    noError(err)
    check(bytes.Equal(downloaded, payload), "streamed download differs")

    paths := []string{path}
    globs := []string{"*.txt"}
    grep := must(c.InspectInstanceGrep(ctx, id, []string{"live-file"}, &contree.InspectInstanceGrepOptions{Path: &paths, Glob: &globs}))
    check(len(grep.Matches) > 0 && strings.Contains(grep.Matches[0].LineText, strings.TrimSpace(string(payload))), "grep missed content")

    archive := must(c.InspectInstanceArchive(ctx, id, "/tmp/client-live"))
    reader := tar.NewReader(archive)
    found = false
    for {
        header, err := reader.Next()
        if err == io.EOF {
            break
        }
        noError(err)
        if strings.HasSuffix(header.Name, "input.txt") {
            check(bytes.Equal(must(io.ReadAll(reader)), payload), "archive content differs")
            found = true
        }
    }
    noError(archive.Close())
    check(found, "archive omitted input.txt")

    replacement := []byte(fmt.Sprintf("replacement-%d\n", time.Now().UnixNano()))
    second := must(c.UploadFile(ctx, bytes.NewReader(replacement)))
    noError(c.ApplyInstanceFile(ctx, id, second.UUID, path, &contree.ApplyInstanceFileOptions{Replace: contree.Some(true)}))
    check(bytes.Equal(must(c.InspectInstanceDownload(ctx, id, path)), replacement), "replacement differs")

    noError(c.OperationSubprocessStdin(ctx, id, 1, "done\n", &contree.OperationSubprocessStdinOptions{Close: contree.Some(true)}))
    success(must(c.WaitOperation(ctx, id)))

    methods := map[string]bool{}
    started := map[string]bool{}
    completed := map[string]bool{}
    uploads := map[string]int64{}
    for event, err := range c.IterOperationEvents(ctx, id, nil) {
        noError(err)
        switch event.Type {
        case contree.OperationEventTypeInspect:
            data, ok := event.Data.(contree.EventDataInspect)
            check(ok, fmt.Sprintf("inspect payload type %T", event.Data))
            spid, present := event.Spid.Value()
            check(present && spid == 0, "inspect event is not operation-scoped")
            methods[data.Method] = true
            if data.Phase == "started" {
                started[data.RequestID] = true
            }
            if data.Phase == "completed" {
                completed[data.RequestID] = true
            }
        case contree.OperationEventTypeFileUpload:
            data, ok := event.Data.(contree.EventDataFileUpload)
            check(ok, fmt.Sprintf("upload payload type %T", event.Data))
            check(data.Status == "success" && data.Error == nil, "upload event reports failure")
            uploads[data.FileUUID] = data.Bytes
        }
    }
    for _, method := range []string{"upload", "list", "open", "archive", "grep"} {
        check(methods[method], "missing inspect method "+method)
    }
    check(len(started) == len(completed), "inspect request/result counts differ")
    for requestID := range started {
        check(completed[requestID], "inspect result missing")
    }
    check(len(uploads) == 2 && uploads[uploaded.UUID] == int64(len(payload)) && uploads[second.UUID] == int64(len(replacement)), "file_upload events differ from transferred files")

    """,
        imports='"archive/tar"; "bytes"; "io"',
    )
