package contree

import (
	"context"
	"errors"
	"fmt"
	"io"
	"net/http"
	"reflect"
	"testing"
	"time"
)

func TestWaitOperationModeSelection(t *testing.T) {
	yes, no := true, false
	for _, outcome := range []OperationStatus{OperationStatusSuccess, OperationStatusFailed, OperationStatusCancelled} {
		for _, mode := range []struct {
			name   string
			kind   string
			follow *bool
			events bool
		}{
			{"auto instance", "instance", nil, true},
			{"auto import", "image_import", nil, false},
			{"unknown kind", "future_kind", nil, true},
			{"force events", "image_import", &yes, true},
			{"force polling", "instance", &no, false},
		} {
			for _, terminal := range []bool{false, true} {
				t.Run(fmt.Sprintf("%s/%s/terminal=%v", outcome, mode.name, terminal), func(t *testing.T) {
					t.Parallel()
					client, mock := newMockClient(t)
					initial := outcome
					if !terminal {
						initial = OperationStatusExecuting
					}
					first := OperationResponse{Kind: Some(mode.kind), Status: Some(initial)}
					final := OperationResponse{Kind: Some(mode.kind), Status: Some(outcome), ResultImageUUID: Some("image"), Error: Some("details")}
					if terminal {
						first = final
					}
					if mode.follow == nil || !*mode.follow {
						requireMock(t, mock.Mock(OpGetOperationStatus, first))
					}
					requireMock(t, mock.Mock(OpGetOperationStatus, final))
					requireMock(t, mock.MockHTTP(OpIterOperationEvents.Name(), MockResponse{
						StatusCode: http.StatusOK,
						Body:       []byte(operationHelperEvent(1, "completion", `{"status":"`+string(outcome)+`","duration_ms":1}`)),
					}))
					result, err := client.WaitOperation(context.Background(), operationHelperID, WaitOperationOptions{FollowEvents: mode.follow})
					requireMock(t, err)
					if !reflect.DeepEqual(result, final) {
						t.Fatalf("result = %#v, want %#v", result, final)
					}
					var want []string
					if mode.follow == nil || !*mode.follow {
						want = append(want, "GetOperationStatus")
					}
					if mode.events {
						want = append(want, "IterOperationEvents", "GetOperationStatus")
					} else if !terminal {
						want = append(want, "GetOperationStatus")
					}
					var got []string
					for _, call := range mock.Calls() {
						got = append(got, call.Operation)
					}
					if !reflect.DeepEqual(got, want) {
						t.Fatalf("requests = %v, want %v", got, want)
					}
				})
			}
		}
	}
}

func TestWaitOperationPollingDeadlineAndErrors(t *testing.T) {
	for _, stage := range []string{"initial read", "poll delay", "second read", "events", "final read"} {
		t.Run(stage, func(t *testing.T) {
			t.Parallel()
			calls := 0
			ctx, cancel := context.WithTimeout(context.Background(), 650*time.Millisecond)
			defer cancel()
			client := newOperationHelperClient(t, func(request *http.Request) (*http.Response, error) {
				calls++
				stall := stage == "initial read" || (stage == "second read" && calls == 2) || (stage == "events" && calls == 2) || (stage == "final read" && calls == 3)
				if stage == "events" && calls == 1 {
					if err := waitForRetry(request.Context(), 400*time.Millisecond); err != nil {
						return nil, err
					}
				}
				if stall {
					<-request.Context().Done()
					return nil, request.Context().Err()
				}
				if stage == "final read" && calls == 2 {
					return operationHelperJSONResponse(request, 200, operationHelperEvent(1, "completion", `{"status":"SUCCESS","duration_ms":1}`)), nil
				}
				kind := "image_import"
				if stage == "events" || stage == "final read" {
					kind = "instance"
				}
				return operationHelperJSONResponse(request, 200, `{"kind":"`+kind+`","status":"EXECUTING"}`), nil
			})
			started := time.Now()
			_, err := client.WaitOperation(ctx, operationHelperID)
			if !errors.Is(err, context.DeadlineExceeded) || time.Since(started) > 1500*time.Millisecond {
				t.Fatalf("deadline error = %v after %v", err, time.Since(started))
			}
		})
	}
	for _, connection := range []bool{false, true} {
		t.Run(fmt.Sprintf("connection=%v", connection), func(t *testing.T) {
			client, mock := newMockClient(t)
			var failure error = &NotFoundError{&APIStatusError{StatusCode: 404}}
			if connection {
				failure = io.ErrUnexpectedEOF
			}
			requireMock(t, mock.MockError(OpGetOperationStatus.Name(), failure))
			_, err := client.WaitOperation(context.Background(), operationHelperID)
			if connection {
				if !errors.Is(err, failure) {
					t.Fatalf("lost connection error: %v", err)
				}
			} else {
				var missing *NotFoundError
				if !errors.As(err, &missing) {
					t.Fatalf("lost HTTP error: %v", err)
				}
			}
			if len(mock.Calls()) != 1 {
				t.Fatalf("unexpected recovery: %v", mock.Calls())
			}
		})
	}
}

func TestWaitOperationRejectsInvalidInputs(t *testing.T) {
	client, mock := newMockClient(t)
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if _, err := client.WaitOperation(ctx, operationHelperID); !errors.Is(err, context.Canceled) {
		t.Fatalf("cancelled wait: %v", err)
	}
	if _, err := client.WaitOperation(nil, operationHelperID); err == nil {
		t.Fatal("nil context accepted")
	}
	if _, err := (*Client)(nil).WaitOperation(context.Background(), operationHelperID); err == nil {
		t.Fatal("nil client accepted")
	}
	if _, err := client.WaitOperation(context.Background(), operationHelperID, WaitOperationOptions{}, WaitOperationOptions{}); err == nil {
		t.Fatal("multiple options accepted")
	}
	if len(mock.Calls()) != 0 {
		t.Fatalf("invalid input made requests: %v", mock.Calls())
	}
}
