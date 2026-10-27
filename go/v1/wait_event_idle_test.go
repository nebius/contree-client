package contree

import (
	"compress/gzip"
	"context"
	"errors"
	"fmt"
	"io"
	"net/http"
	"reflect"
	"strings"
	"sync"
	"testing"
	"testing/synctest"
	"time"
)

func TestWaitOperationCompletionBeforeStatus(t *testing.T) {
	for _, outcome := range []OperationStatus{OperationStatusSuccess, OperationStatusFailed, OperationStatusCancelled} {
		t.Run(string(outcome), func(t *testing.T) {
			synctest.Test(t, func(t *testing.T) {
				reads := 0
				ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
				defer cancel()
				deadline, _ := ctx.Deadline()
				client := newOperationHelperClient(t, func(request *http.Request) (*http.Response, error) {
					if strings.HasSuffix(request.URL.Path, "/events") {
						return operationHelperJSONResponse(request, 200, operationHelperEvent(0, "completion", `{"status":"SUCCESS","duration_ms":1}`)), nil
					}
					if got, _ := request.Context().Deadline(); got != deadline {
						t.Fatalf("status deadline = %v, want %v", got, deadline)
					}
					reads++
					status := outcome
					if reads == 1 {
						status = OperationStatusExecuting
					}
					return operationHelperJSONResponse(request, 200, fmt.Sprintf(`{"status":%q}`, status)), nil
				})
				yes := true
				result, err := client.WaitOperation(ctx, operationHelperID, WaitOperationOptions{FollowEvents: &yes})
				requireMock(t, err)
				if status, _ := result.Status.Value(); status != outcome || reads != 2 {
					t.Fatalf("result = %#v, reads = %d", result, reads)
				}
			})
		})
	}
}

func TestWaitOperationLiveEvents(t *testing.T) {
	for _, ending := range []string{"completion", "deadline", "disconnect"} {
		t.Run(ending, func(t *testing.T) {
			synctest.Test(t, func(t *testing.T) {
				var body *operationHelperBody
				var calls []string
				var writers sync.WaitGroup
				ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
				defer func() {
					cancel()
					writers.Wait()
				}()
				client := newOperationHelperClient(t, func(request *http.Request) (*http.Response, error) {
					if body != nil && !body.closed.Load() {
						t.Fatal("request started before SSE connection was released")
					}
					if !strings.HasSuffix(request.URL.Path, "/events") {
						calls = append(calls, "status")
						return operationHelperJSONResponse(request, 200, `{"status":"SUCCESS"}`), nil
					}
					calls = append(calls, "events")
					if len(calls) != 1 {
						if ending != "disconnect" || request.Header.Get("Last-Event-Id") != "0" {
							t.Fatalf("unexpected reconnect: %v", request.Header)
						}
						return operationHelperJSONResponse(request, 200, operationHelperEvent(1, "completion", `{"status":"SUCCESS","duration_ms":1}`)), nil
					}
					reader, writer := io.Pipe()
					body = &operationHelperBody{Reader: reader}
					go func() {
						<-ctx.Done()
						_ = writer.CloseWithError(ctx.Err())
					}()
					writers.Add(1)
					go func() {
						defer writers.Done()
						_, _ = io.WriteString(writer, operationHelperEvent(0, "stdout", `{"value":"x","encoding":"ascii"}`))
						for range 4 {
							time.Sleep(time.Second)
							if _, err := io.WriteString(writer, ": ping\n\n"); err != nil {
								return
							}
						}
						switch ending {
						case "completion":
							_, _ = io.WriteString(writer, operationHelperEvent(1, "completion", `{"status":"SUCCESS","duration_ms":1}`))
							_ = writer.Close()
						case "disconnect":
							_ = writer.Close()
						case "deadline":
							for {
								time.Sleep(time.Second)
								if _, err := io.WriteString(writer, ": ping\n\n"); err != nil {
									return
								}
							}
						}
					}()
					return operationHelperResponse(request, 200, body), nil
				})
				yes := true
				result, err := client.WaitOperation(ctx, operationHelperID, WaitOperationOptions{FollowEvents: &yes})
				want := []string{"events"}
				if ending == "deadline" {
					if !errors.Is(err, context.DeadlineExceeded) {
						t.Fatalf("error = %v", err)
					}
				} else {
					requireMock(t, err)
					if status, _ := result.Status.Value(); status != OperationStatusSuccess {
						t.Fatalf("result = %#v", result)
					}
					want = []string{"events", "status"}
					if ending == "disconnect" {
						want = []string{"events", "status", "events", "status"}
					}
				}
				if !body.closed.Load() {
					t.Fatal("waiter left SSE connection open")
				}
				if !reflect.DeepEqual(calls, want) {
					t.Fatalf("calls = %v, want %v", calls, want)
				}
			})
		})
	}
}

func TestWaitOperationDelayedStatusKeepsDeadline(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		ctx, cancel := context.WithTimeout(context.Background(), time.Second)
		defer cancel()
		deadline, _ := ctx.Deadline()
		client := newOperationHelperClient(t, func(request *http.Request) (*http.Response, error) {
			if strings.HasSuffix(request.URL.Path, "/events") {
				return operationHelperJSONResponse(request, 200, operationHelperEvent(0, "completion", `{"status":"SUCCESS","duration_ms":1}`)), nil
			}
			if got, _ := request.Context().Deadline(); got != deadline {
				t.Fatalf("status deadline = %v, want %v", got, deadline)
			}
			return operationHelperJSONResponse(request, 200, `{"status":"EXECUTING"}`), nil
		})
		yes := true
		_, err := client.WaitOperation(ctx, operationHelperID, WaitOperationOptions{FollowEvents: &yes})
		if !errors.Is(err, context.DeadlineExceeded) {
			t.Fatalf("error = %v", err)
		}
	})
}

// Retained reads tolerate gaps between payload events.
func TestWaitOperationDelayedRetainedEvents(t *testing.T) {
	for _, interEvent := range []bool{false, true} {
		t.Run(fmt.Sprint(interEvent), func(t *testing.T) {
			synctest.Test(t, func(t *testing.T) {
				retainedRequests := 0
				var writers sync.WaitGroup
				client := newOperationHelperClient(t, func(request *http.Request) (*http.Response, error) {
					if !strings.HasSuffix(request.URL.Path, "/events") {
						return operationHelperJSONResponse(request, 200, `{"status":"SUCCESS"}`), nil
					}
					if request.URL.Query().Get("follow") == "1" {
						return operationHelperJSONResponse(request, 200, ""), nil
					}
					retainedRequests++
					reader, writer := io.Pipe()
					go func() {
						<-request.Context().Done()
						_ = writer.CloseWithError(request.Context().Err())
					}()
					writers.Add(1)
					go func() {
						defer writers.Done()
						defer writer.Close()
						compressed := gzip.NewWriter(writer)
						defer compressed.Close()
						if interEvent {
							_, _ = io.WriteString(compressed, operationHelperEvent(0, "stdout", `{"value":"x","encoding":"ascii"}`))
							_ = compressed.Flush()
						}
						time.Sleep(800 * time.Millisecond)
						_, _ = io.WriteString(compressed, operationHelperEvent(1, "completion", `{"status":"SUCCESS","duration_ms":1}`))
					}()
					response := operationHelperResponse(request, 200, reader)
					response.Header.Set("Content-Encoding", "gzip")
					return response, nil
				})
				ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
				defer func() {
					cancel()
					writers.Wait()
				}()
				yes := true
				result, err := client.WaitOperation(ctx, operationHelperID, WaitOperationOptions{FollowEvents: &yes})
				requireMock(t, err)
				if status, _ := result.Status.Value(); status != OperationStatusSuccess || retainedRequests != 1 {
					t.Fatalf("result = %#v, retained requests = %d", result, retainedRequests)
				}
			})
		})
	}
}
