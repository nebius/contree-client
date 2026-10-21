package contree

import (
	"compress/flate"
	"compress/gzip"
	"context"
	"errors"
	"io"
	"net/http"
	"testing"
	"time"
)

func TestBufferedReadRetriesAndPreservesErrors(t *testing.T) {
	for _, limit := range []int{1, 2, 0} {
		policy := DefaultRetryPolicy()
		policy.MaxAttempts, policy.Delays = limit, []time.Duration{0}
		client, mock := newMockClient(t, WithRetry(policy))
		requireMock(t, mock.MockHTTP(OpGetOperationStatus.Name(), MockResponse{
			StatusCode: 200, Body: []byte(`{"status":`), BodyError: io.ErrUnexpectedEOF,
		}))
		requireMock(t, mock.Mock(OpGetOperationStatus, OperationResponse{Status: Some(OperationStatusSuccess)}))
		operation, err := client.GetOperationStatus(context.Background(), "op", nil)
		if limit == 1 {
			var connection *APIConnectionError
			if !errors.As(err, &connection) || !errors.Is(err, io.ErrUnexpectedEOF) {
				t.Fatalf("read error = %v", err)
			}
			if len(mock.Calls()) != 1 {
				t.Fatal("retried despite attempt limit")
			}
		} else {
			requireMock(t, err)
			if status, _ := operation.Status.Value(); status != OperationStatusSuccess || len(mock.Calls()) != 2 {
				t.Fatalf("status=%v calls=%v", status, mock.Calls())
			}
		}
	}
}

func TestBufferedReadDoesNotRetryUnsafeOperation(t *testing.T) {
	for _, unsafe := range []bool{false, true} {
		policy := DefaultRetryPolicy()
		policy.Delays, policy.RetryUnsafe = []time.Duration{0}, unsafe
		client, mock := newMockClient(t, WithRetry(policy))
		requireMock(t, mock.MockHTTP(OpSpawnInstance.Name(), MockResponse{
			StatusCode: 201, BodyError: io.ErrUnexpectedEOF,
		}))
		requireMock(t, mock.Mock(OpSpawnInstance, InstanceSpawnResponse{UUID: Some("op")}))
		id, err := client.SpawnInstance(context.Background(), "true", "image", SpawnInstanceOptions{})
		if unsafe {
			if err != nil || id != "op" || len(mock.Calls()) != 2 {
				t.Fatalf("unsafe retry = %q, %v, %v", id, err, mock.Calls())
			}
		} else if !errors.Is(err, io.ErrUnexpectedEOF) || len(mock.Calls()) != 1 {
			t.Fatalf("unsafe request was retried: %v, %v", err, mock.Calls())
		}
	}
}

func TestRetryTimeoutIsPerAttempt(t *testing.T) {
	policy := DefaultRetryPolicy()
	policy.MaxAttempts, policy.Delays = 2, []time.Duration{0}
	attempts := 0
	client, err := NewClient("", WithRetry(policy), WithTimeout(20*time.Millisecond), WithHTTPClient(&http.Client{
		Transport: convenienceRoundTripFunc(func(request *http.Request) (*http.Response, error) {
			attempts++
			if attempts == 1 {
				<-request.Context().Done()
				return nil, request.Context().Err()
			}
			if err := request.Context().Err(); err != nil {
				return nil, err
			}
			return operationHelperJSONResponse(request, 200, `{"status":"SUCCESS"}`), nil
		}),
	}))
	requireMock(t, err)
	_, err = client.GetOperationStatus(context.Background(), "op", nil)
	if err != nil || attempts != 2 {
		t.Fatalf("second attempt did not get its own timeout: %v calls=%d", err, attempts)
	}
}

func TestUnboundedRetriesStopOnCancellation(t *testing.T) {
	policy := DefaultRetryPolicy()
	policy.MaxAttempts, policy.Delays = 0, []time.Duration{0}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	attempts := 0
	client, err := NewClient("", WithRetry(policy), WithHTTPClient(&http.Client{
		Transport: convenienceRoundTripFunc(func(request *http.Request) (*http.Response, error) {
			attempts++
			if attempts == 4 {
				cancel()
			}
			return operationHelperJSONResponse(request, 503, `{}`), nil
		}),
	}))
	requireMock(t, err)
	_, err = client.GetOperationStatus(ctx, "op", nil)
	if !errors.Is(err, context.Canceled) || attempts != 4 {
		t.Fatalf("cancel = %v attempts=%d", err, attempts)
	}
}

func TestByteStreamReadErrorsAreNormalized(t *testing.T) {
	client, mock := newMockClient(t)
	requireMock(t, mock.MockStream(OpInspectImageDownload, []byte("partial"), io.ErrUnexpectedEOF))
	body, err := client.InspectImageDownloadStream(context.Background(), "image", "/file")
	requireMock(t, err)
	defer body.Close()
	value, err := io.ReadAll(body)
	var connection *APIConnectionError
	if string(value) != "partial" || !errors.As(err, &connection) || !errors.Is(err, io.ErrUnexpectedEOF) {
		t.Fatalf("stream = %q, %v", value, err)
	}
}

func TestBufferedCorruptionDoesNotRetry(t *testing.T) {
	for _, failure := range []error{gzip.ErrChecksum, flate.CorruptInputError(1)} {
		client, mock := newMockClient(t, WithRetry(DefaultRetryPolicy()))
		requireMock(t, mock.MockHTTP(OpGetOperationStatus.Name(), MockResponse{StatusCode: 200, BodyError: failure}))
		_, err := client.GetOperationStatus(context.Background(), "op", nil)
		var connection *APIConnectionError
		if !errors.Is(err, failure) || errors.As(err, &connection) || len(mock.Calls()) != 1 {
			t.Fatalf("corrupt response was retried or normalized as a connection failure: %v, %v", err, mock.Calls())
		}
	}
}

func TestFollowerUsesConfiguredRetryStatuses(t *testing.T) {
	policy := DefaultRetryPolicy()
	policy.Statuses = []int{http.StatusBadRequest}
	policy.ServerErrors = false
	policy.Delays = []time.Duration{0}
	client, mock := newMockClient(t, WithRetry(policy))
	requireMock(t, mock.MockHTTP(OpIterOperationEvents.Name(), MockResponse{StatusCode: 400}))
	requireMock(t, mock.MockHTTP(OpIterOperationEvents.Name(), MockResponse{StatusCode: 200, Body: []byte(operationHelperEvent(1, "completion", `{"status":"SUCCESS","duration_ms":1}`))}))
	requireMock(t, mock.Mock(OpGetOperationStatus, OperationResponse{Status: Some(OperationStatusSuccess)}))
	for _, err := range client.FollowOperationEvents(context.Background(), "op", nil) {
		requireMock(t, err)
	}
	if len(mock.CallsFor(OpIterOperationEvents.Name())) != 2 {
		t.Fatalf("configured status was not retried: %v", mock.Calls())
	}
}
