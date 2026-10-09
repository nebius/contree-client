package contree_test

const tutorialSubprocessTests = `package examples

import (
    "bytes"
    "context"
    "encoding/json"
    "errors"
    "io"
    "log"
    "reflect"
    "strings"
    "testing"
    "time"

    contree "github.com/nebius/contree-client/go/v1"
)

func subprocessEvents(t *testing.T) []contree.OperationEvent {
    t.Helper()
    spawn := contree.EventDataSpawn{Pid: 42, Command: "id", Args: []string{}}
    exit := contree.EventDataExit{Pid: 42, Code: 0, Signal: -1}
    rows := []struct {
        kind contree.OperationEventType
        spid int64
        data any
    }{
        {contree.OperationEventTypeInit, 0, contree.EventDataInit{}},
        {contree.OperationEventTypeSpawn, 1, spawn},
        {contree.OperationEventTypeSpawn, 2, spawn},
        {contree.OperationEventTypeStdout, 2, contree.NewEventDataStreamFromText("uid=0(root)\n")},
        {contree.OperationEventTypeExit, 9, exit}, // Another process must not end the wait.
        {contree.OperationEventTypeExit, 2, exit},
        {contree.OperationEventTypeExit, 1, exit}, // The parent is not the child.
        {contree.OperationEventTypeCompletion, 0, contree.EventDataCompletion{Status: contree.OperationStatusCancelled}},
    }
    events := make([]contree.OperationEvent, len(rows))
    for i, row := range rows {
        raw, err := json.Marshal(map[string]any{
            "id": i, "ts": "2026-09-04T12:00:00Z", "spid": row.spid,
            "type": row.kind, "data": row.data,
        })
        if err != nil { t.Fatal(err) }
        if err := json.Unmarshal(raw, &events[i]); err != nil { t.Fatal(err) }
    }
    return events
}

func subprocessClient(t *testing.T, events []contree.OperationEvent, createErr error) (*contree.Client, *contree.MockTransport) {
    t.Helper()
    client, mock, err := contree.NewTestClient(contree.WithRetry(contree.RetryPolicy{MaxAttempts: 2, Delays: []time.Duration{time.Millisecond}}))
    if err != nil { t.Fatal(err) }
    configure := func(err error) { t.Helper(); if err != nil { t.Fatal(err) } }
    configure(mock.Mock("SpawnInstance", map[string]string{"uuid": "operation-1"}))
    configure(mock.Mock("IterOperationEvents", events))
    if createErr != nil {
        configure(mock.MockError("OperationSubprocessCreate", createErr))
    } else {
        configure(mock.Mock("OperationSubprocessCreate", int64(2)))
    }
    configure(mock.Mock("CancelOperation", nil))
    // An incomplete event log must terminate without relying on an unconfigured probe.
    configure(mock.Mock("GetOperationStatus", map[string]string{"status": "CANCELLED"}))
    return client, mock
}

func captureSubprocessLog(t *testing.T) *bytes.Buffer {
    t.Helper()
    writer, flags := log.Writer(), log.Flags()
    output := new(bytes.Buffer)
    log.SetOutput(output)
    log.SetFlags(0)
    t.Cleanup(func() { log.SetOutput(writer); log.SetFlags(flags) })
    return output
}

func TestSubprocessExampleUsesOneEventStream(t *testing.T) {
    events := subprocessEvents(t)
    client, mock := subprocessClient(t, events, nil)
    output := captureSubprocessLog(t)
    ctx, cancel := context.WithTimeout(context.Background(), time.Second)
    defer cancel()
    if err := subprocesses(ctx, client, ""); err != nil { t.Fatal(err) }

    var operations []string
    for _, call := range mock.Calls() { operations = append(operations, call.Operation) }
    want := []string{"SpawnInstance", "IterOperationEvents", "OperationSubprocessCreate", "CancelOperation"}
    if !reflect.DeepEqual(operations, want) { t.Fatalf("call order = %v, want %v", operations, want) }
    stream := mock.CallsFor("IterOperationEvents")[0]
    if stream.Query.Get("follow") != "1" || stream.Query.Has("spid") {
        t.Fatalf("event stream query = %v", stream.Query)
    }
    var spawn, child map[string]any
    if err := json.Unmarshal(mock.CallsFor("SpawnInstance")[0].Body, &spawn); err != nil { t.Fatal(err) }
    if err := json.Unmarshal(mock.CallsFor("OperationSubprocessCreate")[0].Body, &child); err != nil { t.Fatal(err) }
    if spawn["command"] != "sleep 60" || spawn["timeout"] != float64(60) || spawn["disposable"] != true {
        t.Fatalf("parent options = %#v", spawn)
    }
    if child["command"] != "id" { t.Fatalf("child options = %#v", child) }

    var logged []contree.OperationEvent
    for _, line := range strings.Split(output.String(), "\n") {
        if raw, ok := strings.CutPrefix(line, "event: "); ok {
            var event contree.OperationEvent
            if err := json.Unmarshal([]byte(raw), &event); err != nil { t.Fatal(err) }
            logged = append(logged, event)
        }
    }
    if !reflect.DeepEqual(logged, events) { t.Fatalf("logged events = %#v, want %#v", logged, events) }
    text := output.String()
    started := strings.Index(text, "started subprocess: 2")
    exited := strings.Index(text, "subprocess exited: 2")
    completed := strings.Index(text, "operation completed: operation-1")
    if started < 0 || exited <= started || completed <= exited {
        t.Fatalf("missing or out-of-order lifecycle messages:\n%s", text)
    }
}

func TestSubprocessExampleRejectsIncompleteLifecycle(t *testing.T) {
    events := subprocessEvents(t)
    for name, frames := range map[string][]contree.OperationEvent{
        "no readiness": {events[0], events[7]},
        "no child exit": {events[0], events[1], events[2], events[3], events[4], events[7]},
        "no completion": events[:7],
    } {
        t.Run(name, func(t *testing.T) {
            client, _ := subprocessClient(t, frames, nil)
            captureSubprocessLog(t)
            ctx, cancel := context.WithTimeout(context.Background(), time.Second)
            defer cancel()
            err := subprocesses(ctx, client, "")
            if name == "no completion" {
                if !errors.Is(err, io.ErrUnexpectedEOF) { t.Fatalf("missing completion error = %v", err) }
                return
            }
            if err == nil || !strings.Contains(err.Error(), "before subprocess exit and operation completion") {
                t.Fatalf("incomplete lifecycle error = %v", err)
            }
        })
    }
}

func TestSubprocessExampleWrapsCreationErrors(t *testing.T) {
    client, mock := subprocessClient(t, subprocessEvents(t), io.ErrUnexpectedEOF)
    output := captureSubprocessLog(t)
    err := subprocesses(context.Background(), client, "")
    if !errors.Is(err, io.ErrUnexpectedEOF) || !strings.Contains(err.Error(), "start subprocess in operation operation-1") {
        t.Fatalf("subprocess error lost its context or cause: %v", err)
    }
    if len(mock.CallsFor("CancelOperation")) != 0 || strings.Contains(output.String(), "operation completed:") {
        t.Fatalf("example reported completion after creation failed:\n%s", output)
    }
}
`
