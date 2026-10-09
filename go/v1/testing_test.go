package contree

import (
	"bytes"
	"compress/gzip"
	"context"
	"crypto/sha256"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"iter"
	"net/http"
	"net/url"
	"reflect"
	"strings"
	"sync"
	"testing"
	"time"
)

func newMockClient(t *testing.T, options ...ClientOption) (*Client, *MockTransport) {
	t.Helper()
	client, mock, err := NewTestClient(options...)
	if err != nil {
		t.Fatal(err)
	}
	return client, mock
}

func requireMock(t *testing.T, err error) {
	t.Helper()
	if err != nil {
		t.Fatal(err)
	}
}

func TestMockClientUsesNormalClientAndSnapshotsModels(t *testing.T) {
	client, mock := newMockClient(t, WithProject("test-project"),
		WithBaseURL("https://example.invalid/sandboxes"),
		WithHTTPClient(&http.Client{Transport: convenienceRoundTripFunc(func(*http.Request) (*http.Response, error) {
			t.Fatal("test client used a caller-supplied network transport")
			return nil, nil
		})}),
	)
	me := WhoAmIResponse{
		Permissions: map[string]bool{"spawn": true}, OperationsStat: map[string]int64{},
	}
	requireMock(t, mock.Mock("WhoAmI", me))
	me.Permissions["spawn"] = false
	// Application code accepts the same concrete type in production and tests.
	canSpawn := func(client *Client) (bool, error) {
		me, err := client.WhoAmI(context.Background())
		return me.Permissions["spawn"], err
	}
	for i := 0; i < 2; i++ {
		allowed, err := canSpawn(client)
		if err != nil || !allowed {
			t.Fatalf("canSpawn = %v, %v", allowed, err)
		}
	}
	calls := mock.CallsFor("WhoAmI")
	if len(calls) != 2 || calls[0].Path != "/sandboxes/v1/whoami" || calls[0].Header.Get("Project") != "test-project" {
		t.Fatalf("calls = %#v", calls)
	}
	if calls[0].Header.Get("Authorization") != "Bearer test-token" {
		t.Fatalf("auth header = %q", calls[0].Header.Get("Authorization"))
	}
	if client, mock, err := NewTestClient(WithTimeout(-time.Second)); err == nil || client != nil || mock != nil {
		t.Fatal("invalid client option did not return an error")
	}
}

func TestMockQueuesAndUnmockedCalls(t *testing.T) {
	client, mock := newMockClient(t)
	_, err := client.GetOperationStatus(context.Background(), "op", nil)
	var missing *NotMockedError
	var connection *APIConnectionError
	if !errors.As(err, &missing) || !errors.As(err, &connection) || missing.Operation != "GetOperationStatus" {
		t.Fatalf("unmocked error = %v", err)
	}
	for _, status := range []OperationStatus{OperationStatusExecuting, OperationStatusSuccess} {
		requireMock(t, mock.Mock("GetOperationStatus", OperationResponse{Status: Some(status)}))
	}
	for _, want := range []OperationStatus{OperationStatusExecuting, OperationStatusSuccess, OperationStatusSuccess} {
		response, err := client.GetOperationStatus(context.Background(), "op", nil)
		status, _ := response.Status.Value()
		if err != nil || status != want {
			t.Fatalf("status = %s, %v; want %s", status, err, want)
		}
	}
	if len(mock.CallsFor("GetOperationStatus")) != 4 || len(mock.CallsFor("unknown")) != 0 {
		t.Fatalf("calls = %#v", mock.Calls())
	}
	request, _ := http.NewRequest(http.MethodGet, "https://example.invalid/not-an-api", nil)
	_, err = mock.RoundTrip(request)
	if !errors.As(err, &missing) || missing.Operation != "" {
		t.Fatalf("unknown route error = %v", err)
	}
}

func TestPublicOperationNamesMatchMocks(t *testing.T) {
	clientType := reflect.TypeOf((*Client)(nil))
	for _, operation := range mockOperations {
		for _, name := range append([]string{operation.name}, operation.aliases...) {
			if _, ok := clientType.MethodByName(name); !ok {
				t.Errorf("mock operation %s has no public Client method", name)
			}
		}
	}
}

func TestMockSpawnInstanceResponseAlias(t *testing.T) {
	for _, name := range []string{"SpawnInstance", "SpawnInstanceWithResponse"} {
		t.Run(name, func(t *testing.T) {
			client, mock := newMockClient(t)
			requireMock(t, mock.Mock(name, map[string]string{"uuid": "first"}))
			requireMock(t, mock.Mock(name, map[string]string{"uuid": "second"}))
			id, err := client.SpawnInstance(context.Background(), "true", "image", SpawnInstanceOptions{})
			requireMock(t, err)
			if id != "first" {
				t.Fatalf("SpawnInstance = %q", id)
			}
			response, err := client.SpawnInstanceWithResponse(context.Background(), "true", "image", nil)
			requireMock(t, err)
			if id, ok := response.UUID.Value(); !ok || id != "second" {
				t.Fatalf("SpawnInstanceWithResponse UUID = %q, %v", id, ok)
			}
			calls := mock.CallsFor("SpawnInstance")
			if len(calls) != 2 || !reflect.DeepEqual(calls, mock.CallsFor("SpawnInstanceWithResponse")) {
				t.Fatalf("spawn methods have different call logs: %#v", mock.Calls())
			}
			for _, call := range calls {
				if call.Operation != "SpawnInstance" || call.Method != http.MethodPost || call.Path != "/v1/instances" {
					t.Fatalf("spawn call = %#v", call)
				}
			}
		})
	}
}

func TestMockSpawnInstanceRecordsWireValuesAndIndependentCalls(t *testing.T) {
	client, mock := newMockClient(t)
	requireMock(t, mock.Mock("SpawnInstance", InstanceSpawnResponse{UUID: Some("op")}))
	id, err := client.SpawnInstance(context.Background(), "echo hi", "tag:ubuntu:latest", SpawnInstanceOptions{
		Shell: true, Timeout: time.Minute,
	})
	if err != nil || id != "op" {
		t.Fatalf("SpawnInstance = %q, %v", id, err)
	}
	call := mock.CallsFor("SpawnInstance")[0]
	var body map[string]any
	requireMock(t, json.Unmarshal(call.Body, &body))
	if body["command"] != "echo hi" || body["shell"] != true || body["timeout"] != float64(60) {
		t.Fatalf("body = %#v", body)
	}
	call.Body[0] = '!'
	call.Header.Set("Authorization", "changed")
	if snapshot := mock.Calls()[0]; snapshot.Body[0] != '{' || snapshot.Header.Get("Authorization") == "changed" {
		t.Fatal("call snapshot aliases the recorded request")
	}

	requireMock(t, mock.Mock("GetOperationStatus", OperationResponse{}))
	operationID := "part/with ?#%/v1"
	_, err = client.GetOperationStatus(context.Background(), operationID, &GetOperationStatusOptions{Inflight: true})
	requireMock(t, err)
	call = mock.CallsFor("GetOperationStatus")[0]
	if call.PathParams["operationId"] != operationID || call.Query.Get("inflight") != "1" {
		t.Fatalf("recorded parameters = %#v, %#v", call.PathParams, call.Query)
	}
	call.PathParams["operationId"] = "changed"
	call.Query.Set("inflight", "changed")
	if snapshot := mock.CallsFor("GetOperationStatus")[0]; snapshot.PathParams["operationId"] != operationID || snapshot.Query.Get("inflight") != "1" {
		t.Fatal("call snapshot aliases the recorded parameters")
	}
}

func TestMockResponseKinds(t *testing.T) {
	ctx := context.Background()
	client, mock := newMockClient(t)
	requireMock(t, mock.Mock("CancelOperation", nil))
	requireMock(t, client.CancelOperation(ctx, "op"))
	requireMock(t, mock.Mock("CheckFileExists", false))
	requireMock(t, mock.Mock("CheckFileExists", true))
	for _, want := range []bool{false, true, true} {
		got, err := client.CheckFileExists(ctx, "digest")
		if err != nil || got != want {
			t.Fatalf("CheckFileExists = %v, %v; want %v", got, err, want)
		}
	}
	requireMock(t, mock.Mock("InspectFindImageByTag", convenienceImageUUID))
	image, err := client.ResolveImage(ctx, "tag:ubuntu:latest")
	if err != nil || image != convenienceImageUUID {
		t.Fatalf("ResolveImage = %s, %v", image, err)
	}
	requireMock(t, mock.Mock("ImportImage", "import-op"))
	id, err := client.ImportImage(ctx, ImageImportRegistry{}, nil)
	if err != nil || id != "import-op" {
		t.Fatalf("ImportImage = %s, %v", id, err)
	}
	requireMock(t, mock.Mock("OperationSubprocessCreate", int64(42)))
	spid, err := client.OperationSubprocessCreate(ctx, "op", "cat", nil)
	if err != nil || spid != 42 {
		t.Fatalf("OperationSubprocessCreate = %d, %v", spid, err)
	}
	requireMock(t, mock.Mock("OperationSubprocessStdin", nil))
	requireMock(t, client.OperationSubprocessStdin(ctx, "op", spid, "hello", nil))
	call := mock.CallsFor("OperationSubprocessStdin")[0]
	if call.PathParams["spid"] != "42" || !bytes.Contains(call.Body, []byte(`"value":"hello"`)) {
		t.Fatalf("subprocess stdin call = %#v", call)
	}
}

func TestMockErrorsAndRetries(t *testing.T) {
	policy := DefaultRetryPolicy()
	policy.Delays = []time.Duration{0}
	policy.MaxAttempts = 2
	client, mock := newMockClient(t, WithRetry(policy))
	requireMock(t, mock.MockError("GetOperationStatus", &RateLimitError{&APIStatusError{
		StatusCode: 429, Details: "slow down",
	}}))
	requireMock(t, mock.Mock("GetOperationStatus", OperationResponse{Status: Some(OperationStatusSuccess)}))
	_, err := client.GetOperationStatus(context.Background(), "op", nil)
	if err != nil || len(mock.CallsFor("GetOperationStatus")) != 2 {
		t.Fatalf("retry = %v; calls = %#v", err, mock.Calls())
	}
	requireMock(t, mock.MockError("GetFile", &NotFoundError{&APIStatusError{
		StatusCode: 404, Details: "not found", RequestID: "request-1",
	}}))
	_, err = client.GetFile(context.Background(), "digest")
	var missing *NotFoundError
	if !errors.As(err, &missing) || missing.RequestID != "request-1" || missing.Details != "not found" {
		t.Fatalf("HTTP error = %#v", err)
	}
	requireMock(t, mock.MockError("SpawnInstance", io.ErrUnexpectedEOF))
	_, err = client.SpawnInstance(context.Background(), "true", "image", SpawnInstanceOptions{})
	var connection *APIConnectionError
	if !errors.As(err, &connection) || !errors.Is(err, io.ErrUnexpectedEOF) || len(mock.CallsFor("SpawnInstance")) != 1 {
		t.Fatalf("transport error = %v", err)
	}
}

func TestMockHTTPClonesInputAndDecodesRealResponses(t *testing.T) {
	client, mock := newMockClient(t)
	body := []byte(`{"uuid":"original"}`)
	header := http.Header{"X-Request-Id": {"original"}}
	requireMock(t, mock.MockHTTP("SpawnInstance", MockResponse{StatusCode: 201, Header: header, Body: body}))
	body[0] = '!'
	header.Set("X-Request-Id", "changed")
	for i := 0; i < 2; i++ {
		id, err := client.SpawnInstance(context.Background(), "true", "image", SpawnInstanceOptions{})
		if err != nil || id != "original" {
			t.Fatalf("SpawnInstance = %q, %v", id, err)
		}
	}
	requireMock(t, mock.MockHTTP("WhoAmI", MockResponse{StatusCode: 200, Body: []byte(`{}`)}))
	if _, err := client.WhoAmI(context.Background()); err == nil {
		t.Fatal("invalid model bypassed the real JSON decoder")
	}
}

func TestMockFileAndPaginationHelpers(t *testing.T) {
	client, mock := newMockClient(t)
	content := "hello\n"
	digest := fmt.Sprintf("%x", sha256.Sum256([]byte(content)))
	requireMock(t, mock.MockError("GetFile", &NotFoundError{&APIStatusError{StatusCode: 404}}))
	requireMock(t, mock.Mock("UploadFile", FileResponse{UUID: "file", SHA256: digest, Size: int64(len(content))}))
	file, err := client.EnsureFile(context.Background(), strings.NewReader(content), nil)
	if err != nil || file.UUID != "file" {
		t.Fatalf("EnsureFile = %#v, %v", file, err)
	}
	if mock.CallsFor("GetFile")[0].PathParams["sha256"] != digest || string(mock.CallsFor("UploadFile")[0].Body) != content {
		t.Fatalf("file calls = %#v", mock.Calls())
	}
	requireMock(t, mock.Mock("ListFiles", FilesListResponse{Files: Some([]File{{UUID: "file"}})}))
	requireMock(t, mock.Mock("ListFiles", FilesListResponse{Files: Some([]File{})}))
	var ids []string
	for file, err := range client.IterFiles(context.Background(), &IterFilesOptions{PageSize: 1}) {
		requireMock(t, err)
		ids = append(ids, file.UUID)
	}
	if !reflect.DeepEqual(ids, []string{"file"}) {
		t.Fatalf("pagination = %v", ids)
	}
	calls := mock.CallsFor("ListFiles")
	if len(calls) != 2 || calls[0].Query.Get("offset") != "0" || calls[1].Query.Get("offset") != "1" {
		t.Fatalf("pagination calls = %#v", calls)
	}
}

func TestMockByteStreamsAndAliases(t *testing.T) {
	client, mock := newMockClient(t)
	requireMock(t, mock.MockStream("InspectImageDownloadStream", [][]byte{[]byte("abc"), []byte("def")}, io.ErrUnexpectedEOF))
	for i := 0; i < 2; i++ {
		stream, err := client.InspectImageDownloadStream(context.Background(), "image", "/file")
		requireMock(t, err)
		body, err := io.ReadAll(stream)
		stream.Close()
		if string(body) != "abcdef" || !errors.Is(err, io.ErrUnexpectedEOF) {
			t.Fatalf("stream = %q, %v", body, err)
		}
	}
	if len(mock.CallsFor("InspectImageDownload")) != 2 || len(mock.CallsFor("InspectImageDownloadStream")) != 2 {
		t.Fatal("stream alias did not share the canonical log")
	}
	var compressed bytes.Buffer
	writer := gzip.NewWriter(&compressed)
	_, err := writer.Write([]byte("archive"))
	requireMock(t, err)
	requireMock(t, writer.Close())
	requireMock(t, mock.MockHTTP("InspectImageArchive", MockResponse{
		StatusCode: 200, Header: http.Header{"Content-Encoding": {"gzip"}}, Body: compressed.Bytes(),
	}))
	decoded, err := client.InspectImageArchive(context.Background(), "image", "/")
	requireMock(t, err)
	body, err := io.ReadAll(decoded)
	decoded.Close()
	if err != nil || string(body) != "archive" {
		t.Fatalf("decoded archive = %q, %v", body, err)
	}
	raw, err := client.InspectImageArchiveRaw(context.Background(), "image", "/")
	requireMock(t, err)
	body, err = io.ReadAll(raw)
	raw.Close()
	if err != nil || !bytes.Equal(body, compressed.Bytes()) {
		t.Fatalf("raw archive = %q, %v", body, err)
	}
}

func TestMockSSEAndFollowerReconnect(t *testing.T) {
	client, mock := newMockClient(t)
	first := OperationEvent{ID: 1, Type: OperationEventTypeStdout, Data: NewEventDataStreamFromText("hello\n")}
	last := OperationEvent{ID: 2, Type: OperationEventTypeCompletion, Data: EventDataCompletion{Status: OperationStatusSuccess}}
	requireMock(t, mock.MockStream("IterOperationEvents", []OperationEvent{first}, io.ErrUnexpectedEOF))
	next, stop := iter.Pull2(client.IterOperationEvents(context.Background(), "op", nil))
	defer stop()
	event, err, ok := next()
	if !ok || err != nil || event.ID != 1 {
		t.Fatalf("first SSE event = %#v, %v", event, err)
	}
	if _, err, ok := next(); !ok || !errors.Is(err, io.ErrUnexpectedEOF) {
		t.Fatalf("SSE read error = %v", err)
	}
	stop()
	// The first outcome is sticky until another one is queued. Reconnect uses
	// the final event and records the real resume query/header from the helper.
	requireMock(t, mock.Mock("IterOperationEvents", []OperationEvent{last}))
	requireMock(t, mock.Mock("GetOperationStatus", OperationResponse{Status: Some(OperationStatusExecuting)}))
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	var ids []int64
	for event, err := range client.FollowOperationEvents(ctx, "op", nil) {
		requireMock(t, err)
		ids = append(ids, event.ID)
	}
	if !reflect.DeepEqual(ids, []int64{1, 2}) {
		t.Fatalf("follower = %v", ids)
	}
	calls := mock.CallsFor("IterOperationEvents")
	if len(calls) != 3 || calls[2].Header.Get("Last-Event-ID") != "1" {
		t.Fatalf("event calls = %#v", calls)
	}
}

func TestMockContextAndBodyOwnership(t *testing.T) {
	client, mock := newMockClient(t)
	requireMock(t, mock.Mock("InspectImageArchive", "abcdef"))
	ctx, cancel := context.WithCancel(context.Background())
	stream, err := client.InspectImageArchive(ctx, "image", "/")
	requireMock(t, err)
	cancel()
	if _, err := stream.Read(make([]byte, 1)); !errors.Is(err, context.Canceled) {
		t.Fatalf("stream cancellation = %v", err)
	}
	requireMock(t, stream.Close())
	if _, err := stream.Read(make([]byte, 1)); !errors.Is(err, io.ErrClosedPipe) {
		t.Fatalf("closed body = %v", err)
	}
	before := len(mock.Calls())
	_, err = client.WhoAmI(ctx)
	if !errors.Is(err, context.Canceled) || len(mock.Calls()) != before {
		t.Fatalf("cancelled call = %v", err)
	}
	body := &trackingBody{Reader: strings.NewReader("upload")}
	request, _ := http.NewRequest(http.MethodPost, "https://contree.test/v1/files", body)
	_, _ = mock.RoundTrip(request)
	if !body.closed {
		t.Fatal("request body was not closed")
	}
}

func TestMockConcurrentCalls(t *testing.T) {
	client, mock := newMockClient(t)
	requireMock(t, mock.Mock("SpawnInstance", InstanceSpawnResponse{UUID: Some("op")}))
	var group sync.WaitGroup
	for i := 0; i < 30; i++ {
		group.Add(1)
		go func() {
			defer group.Done()
			id, err := client.SpawnInstance(context.Background(), "true", "image", SpawnInstanceOptions{})
			if err != nil || id != "op" {
				t.Errorf("SpawnInstance = %q, %v", id, err)
			}
			_ = mock.Calls()
			if err := mock.Mock("CancelOperation", nil); err != nil {
				t.Error(err)
			}
		}()
	}
	group.Wait()
	if len(mock.CallsFor("SpawnInstance")) != 30 {
		t.Fatalf("calls = %d", len(mock.Calls()))
	}
}

func TestMockRejectsInvalidConfiguration(t *testing.T) {
	var mock MockTransport
	for name, configure := range map[string]func() error{
		"unknown operation": func() error { return mock.Mock("WhoAmI_typo", nil) },
		"helper name":       func() error { return mock.Mock("EnsureFile", "file") },
		"wrong boolean":     func() error { return mock.Mock("CheckFileExists", "false") },
		"wrong empty":       func() error { return mock.Mock("CancelOperation", true) },
		"wrong bytes":       func() error { return mock.Mock("InspectImageArchive", 42) },
		"wrong SSE":         func() error { return mock.Mock("IterOperationEvents", "data") },
		"empty location":    func() error { return mock.Mock("InspectFindImageByTag", "") },
		"invalid JSON":      func() error { return mock.Mock("GetOperationStatus", make(chan int)) },
		"nil error":         func() error { return mock.MockError("GetFile", nil) },
		"non-stream":        func() error { return mock.MockStream("WhoAmI", nil, nil) },
		"invalid status":    func() error { return mock.MockHTTP("WhoAmI", MockResponse{StatusCode: 0}) },
	} {
		t.Run(name, func(t *testing.T) {
			if err := configure(); err == nil {
				t.Fatal("invalid configuration was accepted")
			}
		})
	}
	if len(mock.mocks) != 0 {
		t.Fatal("invalid configuration changed the outcome queues")
	}
}

func TestMockMetadataRoutesEveryGeneratedOperation(t *testing.T) {
	clientType := reflect.TypeOf((*Client)(nil))
	for _, op := range mockOperations {
		t.Run(op.name, func(t *testing.T) {
			if _, ok := clientType.MethodByName(op.name); !ok {
				t.Fatalf("metadata method %s does not exist", op.name)
			}
			path := op.path
			params := map[string]string{}
			for _, part := range strings.Split(op.path, "/") {
				if strings.HasPrefix(part, "{") {
					name := part[1 : len(part)-1]
					params[name] = "a/b ?#%/v1"
					path = strings.ReplaceAll(path, part, url.PathEscape(params[name]))
				}
			}
			var mock MockTransport
			requireMock(t, mock.MockHTTP(op.name, MockResponse{StatusCode: 200}))
			request, _ := http.NewRequest(op.method, "https://example.invalid/prefix/v1"+path, nil)
			response, err := mock.RoundTrip(request)
			requireMock(t, err)
			response.Body.Close()
			call := mock.Calls()[0]
			if call.Operation != op.name || !reflect.DeepEqual(call.PathParams, params) {
				t.Fatalf("route = %#v", call)
			}
			for _, alias := range op.aliases {
				if _, ok := clientType.MethodByName(alias); !ok {
					t.Fatalf("alias method %s does not exist", alias)
				}
				if len(mock.CallsFor(alias)) != 1 {
					t.Fatalf("alias %s has no canonical call", alias)
				}
			}
		})
	}
}
