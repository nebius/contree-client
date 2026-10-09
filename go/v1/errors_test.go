package contree

import (
	"context"
	"errors"
	"strconv"
	"strings"
	"testing"
	"time"
)

func TestAPIConnectionErrorPreservesCauseAndTimeout(t *testing.T) {
	t.Parallel()

	err := &APIConnectionError{
		Method:   "GET",
		URL:      "https://example.test/v1/images",
		TimedOut: true,
		Err:      context.DeadlineExceeded,
	}
	if !err.Timeout() {
		t.Fatal("Timeout returned false")
	}
	if !errors.Is(err, context.DeadlineExceeded) {
		t.Fatalf("error does not unwrap its cause: %v", err)
	}
	if !strings.Contains(err.Error(), "request timed out") {
		t.Fatalf("Error() = %q", err.Error())
	}
}

func TestAPIStatusErrorPreservesParsedMetadata(t *testing.T) {
	t.Parallel()

	body := []byte(`{"error":{"code":"invalid","message":"bad input"},"traceback":["first",2]}`)
	retryAfter := time.Duration(0)
	status := &APIStatusError{
		StatusCode: 400,
		Method:     "POST",
		URL:        "https://example.test/v1/instances",
		RequestID:  "request-1",
		Body:       body,
		Details:    decodeErrorDetails(body),
		RetryAfter: &retryAfter,
	}
	err := newStatusError(status)

	var badRequest *BadRequestError
	if !errors.As(err, &badRequest) {
		t.Fatalf("error = %T, want *BadRequestError", err)
	}
	var base *APIStatusError
	if !errors.As(err, &base) {
		t.Fatalf("errors.As could not reach *APIStatusError through %T", err)
	}
	if base != status {
		t.Fatal("errors.As returned a different APIStatusError")
	}
	if base.RetryAfter == nil || *base.RetryAfter != 0 {
		t.Fatalf("RetryAfter = %v, want explicit zero", base.RetryAfter)
	}
	if len(base.Traceback) != 2 || base.Traceback[0] != "first" || base.Traceback[1] != "2" {
		t.Fatalf("Traceback = %#v", base.Traceback)
	}
	details, ok := base.Details.(map[string]any)
	if !ok || details["code"] != "invalid" {
		t.Fatalf("Details = %#v", base.Details)
	}
	if !strings.Contains(err.Error(), "bad input") || !strings.Contains(err.Error(), "request-1") {
		t.Fatalf("Error() = %q", err.Error())
	}

	body[0] = 'x'
	if base.Body[0] != '{' {
		t.Fatal("APIStatusError did not retain its own body copy")
	}
}

func TestSetRetryAfterDistinguishesAbsentAndZero(t *testing.T) {
	t.Parallel()

	status := &APIStatusError{}
	setRetryAfter(status, 0, true)
	if status.RetryAfter == nil || *status.RetryAfter != 0 {
		t.Fatalf("RetryAfter = %v, want explicit zero", status.RetryAfter)
	}
	setRetryAfter(status, time.Second, false)
	if status.RetryAfter != nil {
		t.Fatalf("RetryAfter = %v, want nil", status.RetryAfter)
	}
}

func TestNewStatusErrorClassifiesStatusesAndUnwrapsBase(t *testing.T) {
	t.Parallel()

	tests := []struct {
		status int
		match  func(error) bool
	}{
		{400, func(err error) bool { var target *BadRequestError; return errors.As(err, &target) }},
		{401, func(err error) bool { var target *AuthenticationError; return errors.As(err, &target) }},
		{403, func(err error) bool { var target *PermissionDeniedError; return errors.As(err, &target) }},
		{404, func(err error) bool { var target *NotFoundError; return errors.As(err, &target) }},
		{409, func(err error) bool { var target *ConflictError; return errors.As(err, &target) }},
		{410, func(err error) bool { var target *GoneError; return errors.As(err, &target) }},
		{422, func(err error) bool { var target *UnprocessableEntityError; return errors.As(err, &target) }},
		{425, func(err error) bool { var target *TooEarlyError; return errors.As(err, &target) }},
		{429, func(err error) bool { var target *RateLimitError; return errors.As(err, &target) }},
		{500, func(err error) bool { var target *ServerError; return errors.As(err, &target) }},
		{599, func(err error) bool { var target *ServerError; return errors.As(err, &target) }},
	}
	for _, test := range tests {
		test := test
		t.Run(strconv.Itoa(test.status), func(t *testing.T) {
			t.Parallel()
			err := newStatusError(&APIStatusError{StatusCode: test.status})
			if !test.match(err) {
				t.Fatalf("status %d classified as %T", test.status, err)
			}
			var base *APIStatusError
			if !errors.As(err, &base) || base.StatusCode != test.status {
				t.Fatalf("base status through %T = %#v", err, base)
			}
		})
	}

	status := &APIStatusError{StatusCode: 600}
	if err := newStatusError(status); err != status {
		t.Fatalf("status 600 classified as %T", err)
	}
}

func TestDecodeErrorDetailsFallsBackToText(t *testing.T) {
	t.Parallel()

	details := decodeErrorDetails([]byte("backend unavailable"))
	if details != "backend unavailable" {
		t.Fatalf("details = %#v", details)
	}
}
