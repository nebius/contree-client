package contree

import (
	"context"
	"errors"
	"io"
	"iter"
	"net/http"
	"reflect"
	"strings"
	"testing"
)

type iteratorBody struct {
	io.Reader
	closes   int
	closeErr error
}

func (b *iteratorBody) Close() error {
	b.closes++
	return b.closeErr
}

var eventIterators = map[string]func(*Client, context.Context) iter.Seq2[OperationEvent, error]{
	"raw": func(client *Client, ctx context.Context) iter.Seq2[OperationEvent, error] {
		return client.IterOperationEvents(ctx, operationHelperID, nil)
	},
	"follow": func(client *Client, ctx context.Context) iter.Seq2[OperationEvent, error] {
		return client.FollowOperationEvents(ctx, operationHelperID, nil)
	},
}

func iteratorTestClient(t *testing.T, source string, closeErr error) (*Client, *[]*iteratorBody) {
	t.Helper()
	var bodies []*iteratorBody
	client := newOperationHelperClient(t, func(request *http.Request) (*http.Response, error) {
		if !strings.HasSuffix(request.URL.Path, "/events") {
			t.Fatalf("unexpected request after iteration stopped: %s", request.URL.Path)
		}
		body := &iteratorBody{Reader: strings.NewReader(source), closeErr: closeErr}
		bodies = append(bodies, body)
		return operationHelperResponse(request, http.StatusOK, body), nil
	})
	return client, &bodies
}

func TestEventIteratorsAreLazyAndCloseOnEarlyExit(t *testing.T) {
	for name, events := range eventIterators {
		for _, exit := range []string{"break", "return", "panic", "pull stop"} {
			t.Run(name+"/"+exit, func(t *testing.T) {
				source := operationHelperEvent(1, "stdout", `{"value":"hello","encoding":"ascii"}`) + "data: invalid\n\n"
				client, bodies := iteratorTestClient(t, source, errors.New("close failed"))
				sequence := events(client, context.Background())
				if len(*bodies) != 0 {
					t.Fatal("iterator construction opened a response")
				}
				for traversal := range 2 {
					func() {
						if exit == "panic" {
							defer func() {
								if got := recover(); got != "consumer panic" {
									t.Errorf("panic = %v", got)
								}
							}()
						}
						if exit == "pull stop" {
							next, stop := iter.Pull2(sequence)
							defer stop()
							event, err, ok := next()
							if !ok || err != nil || event.ID != 1 {
								t.Fatalf("first event = %#v, %v, %v", event, err, ok)
							}
							return
						}
						for event, err := range sequence {
							if err != nil || event.ID != 1 {
								t.Fatalf("first event = %#v, %v", event, err)
							}
							if exit == "panic" {
								panic("consumer panic")
							}
							if exit == "return" {
								return
							}
							break
						}
					}()
					if len(*bodies) != traversal+1 || (*bodies)[traversal].closes != 1 {
						t.Fatalf("traversal %d did not close exactly one response", traversal)
					}
				}
			})
		}
	}
}

func TestEventIteratorsYieldOneErrorAndClose(t *testing.T) {
	for name, events := range eventIterators {
		t.Run(name, func(t *testing.T) {
			source := operationHelperEvent(1, "stdout", `{"value":"hello","encoding":"ascii"}`) + "data: invalid\n\n"
			client, bodies := iteratorTestClient(t, source, nil)
			items, failures := 0, 0
			for event, err := range events(client, context.Background()) {
				if err != nil {
					failures++
					if !reflect.DeepEqual(event, OperationEvent{}) {
						t.Fatalf("nonzero error event: %#v", event)
					}
					continue
				}
				items++
			}
			if items != 1 || failures != 1 || len(*bodies) != 1 || (*bodies)[0].closes != 1 {
				t.Fatalf("items=%d errors=%d responses=%d", items, failures, len(*bodies))
			}
		})
	}
}

func TestEventIteratorsCloseOnCompletionAndReportCloseErrors(t *testing.T) {
	for name, events := range eventIterators {
		for _, closeErr := range []error{nil, errors.New("close failed")} {
			caseName := "success"
			if closeErr != nil {
				caseName = "close failure"
			}
			t.Run(name+"/"+caseName, func(t *testing.T) {
				source := operationHelperEvent(1, "completion", `{"status":"SUCCESS","duration_ms":1}`)
				client, bodies := iteratorTestClient(t, source, closeErr)
				items, failures := 0, 0
				for event, err := range events(client, context.Background()) {
					if err != nil {
						failures++
						if !errors.Is(err, closeErr) {
							t.Fatalf("close error = %v", err)
						}
						continue
					}
					items++
					if event.Type != OperationEventTypeCompletion {
						t.Fatalf("event = %#v", event)
					}
				}
				wantFailures := 0
				if closeErr != nil {
					wantFailures = 1
				}
				if items != 1 || failures != wantFailures || (*bodies)[0].closes != 1 {
					t.Fatalf("items=%d errors=%d closes=%d", items, failures, (*bodies)[0].closes)
				}
			})
		}
	}
}

func TestEventIteratorsHonorCancellationBetweenEvents(t *testing.T) {
	for name, events := range eventIterators {
		t.Run(name, func(t *testing.T) {
			source := operationHelperEvent(1, "stdout", `{"value":"hello","encoding":"ascii"}`) + operationHelperEvent(2, "stdout", `{"value":"more","encoding":"ascii"}`)
			client, bodies := iteratorTestClient(t, source, nil)
			ctx, cancel := context.WithCancel(context.Background())
			defer cancel()
			items, failures := 0, 0
			for _, err := range events(client, ctx) {
				if err != nil {
					failures++
					if !errors.Is(err, context.Canceled) {
						t.Fatal(err)
					}
					continue
				}
				items++
				cancel()
			}
			if items != 1 || failures != 1 || (*bodies)[0].closes != 1 {
				t.Fatalf("items=%d errors=%d closes=%d", items, failures, (*bodies)[0].closes)
			}
		})
	}
}

func TestEventIteratorsYieldOpenErrors(t *testing.T) {
	for name, events := range eventIterators {
		t.Run(name, func(t *testing.T) {
			client, mock := newMockClient(t)
			requireMock(t, mock.MockHTTP("IterOperationEvents", MockResponse{StatusCode: http.StatusUnauthorized}))
			count := 0
			for event, err := range events(client, context.Background()) {
				count++
				var denied *AuthenticationError
				if !errors.As(err, &denied) || !reflect.DeepEqual(event, OperationEvent{}) {
					t.Fatalf("open failure = %#v, %v", event, err)
				}
			}
			if count != 1 || len(mock.Calls()) != 1 {
				t.Fatalf("errors=%d requests=%d", count, len(mock.Calls()))
			}
		})
	}
}

func TestGeneratedListMethodsReturnNativeIterators(t *testing.T) {
	client, mock := newMockClient(t)
	requireMock(t, mock.Mock("ListImages", map[string]any{"images": []map[string]string{{"uuid": "image"}}}))
	requireMock(t, mock.Mock("ListFiles", map[string]any{"files": []File{{UUID: "file"}}}))
	requireMock(t, mock.Mock("ListOperations", []map[string]string{{"uuid": "operation"}}))
	var images iter.Seq2[Image, error] = client.IterImages(context.Background(), nil)
	var files iter.Seq2[File, error] = client.IterFiles(context.Background(), nil)
	var operations iter.Seq2[OperationSummary, error] = client.IterOperations(context.Background(), nil)
	if len(mock.Calls()) != 0 {
		t.Fatal("iterator construction sent requests")
	}
	for range 2 {
		imageItems, err := collectPages(images)
		requireMock(t, err)
		fileItems, err := collectPages(files)
		requireMock(t, err)
		operationItems, err := collectPages(operations)
		requireMock(t, err)
		if len(imageItems) != 1 || len(fileItems) != 1 || len(operationItems) != 1 {
			t.Fatalf("item counts = %d, %d, %d", len(imageItems), len(fileItems), len(operationItems))
		}
	}
	if len(mock.Calls()) != 6 {
		t.Fatalf("requests = %d", len(mock.Calls()))
	}
	for _, call := range mock.Calls() {
		if call.Query.Get("offset") != "0" {
			t.Fatalf("iterator reused its offset: %v", call.Query)
		}
	}
}
