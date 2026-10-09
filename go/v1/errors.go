package contree

import (
	"bytes"
	"encoding/json"
	"fmt"
	"strings"
	"time"
)

// APIConnectionError reports a connection or response-read failure.
// TimedOut distinguishes deadline and socket timeout failures.
type APIConnectionError struct {
	Method   string
	URL      string
	TimedOut bool
	Err      error
}

func (e *APIConnectionError) Error() string {
	if e == nil {
		return "contree: connection failed"
	}
	kind := "connection failed"
	if e.TimedOut {
		kind = "request timed out"
	}
	if e.Err == nil {
		return fmt.Sprintf("contree: %s: %s %s", kind, e.Method, e.URL)
	}
	return fmt.Sprintf("contree: %s: %s %s: %v", kind, e.Method, e.URL, e.Err)
}

// Timeout reports whether the transport failure was caused by a deadline or
// socket timeout.
func (e *APIConnectionError) Timeout() bool {
	return e != nil && e.TimedOut
}

// Unwrap returns the original transport error.
func (e *APIConnectionError) Unwrap() error {
	if e == nil {
		return nil
	}
	return e.Err
}

// APIStatusError reports an HTTP response that the operation did not accept.
// Body contains at most the runtime error-body limit. Details contains the
// decoded "error" property when present, or the decoded response otherwise.
// A non-nil RetryAfter preserves the header even when its value is zero.
type APIStatusError struct {
	StatusCode int
	Method     string
	URL        string
	RequestID  string
	Body       []byte
	Details    any
	Traceback  []string
	RetryAfter *time.Duration
}

func (e *APIStatusError) Error() string {
	if e == nil {
		return "contree: unexpected HTTP status"
	}
	message := fmt.Sprintf(
		"contree: unexpected HTTP status %d for %s %s",
		e.StatusCode,
		e.Method,
		e.URL,
	)
	if e.RequestID != "" {
		message += fmt.Sprintf(" (request ID %s)", e.RequestID)
	}
	if detail := errorDetail(e.Details); detail != "" {
		message += ": " + detail
	}
	return message
}

// BadRequestError is returned for an unexpected HTTP 400 response.
type BadRequestError struct{ *APIStatusError }

// AuthenticationError is returned for an unexpected HTTP 401 response.
type AuthenticationError struct{ *APIStatusError }

// PermissionDeniedError is returned for an unexpected HTTP 403 response.
type PermissionDeniedError struct{ *APIStatusError }

// NotFoundError is returned for an unexpected HTTP 404 response.
type NotFoundError struct{ *APIStatusError }

// ConflictError is returned for an unexpected HTTP 409 response.
type ConflictError struct{ *APIStatusError }

// GoneError is returned for an unexpected HTTP 410 response.
type GoneError struct{ *APIStatusError }

// UnprocessableEntityError is returned for an unexpected HTTP 422 response.
type UnprocessableEntityError struct{ *APIStatusError }

// TooEarlyError is returned for an unexpected HTTP 425 response.
type TooEarlyError struct{ *APIStatusError }

// RateLimitError is returned for an unexpected HTTP 429 response.
type RateLimitError struct{ *APIStatusError }

// ServerError is returned for an unexpected HTTP 5xx response.
type ServerError struct{ *APIStatusError }

func (e *BadRequestError) Unwrap() error          { return e.APIStatusError }
func (e *AuthenticationError) Unwrap() error      { return e.APIStatusError }
func (e *PermissionDeniedError) Unwrap() error    { return e.APIStatusError }
func (e *NotFoundError) Unwrap() error            { return e.APIStatusError }
func (e *ConflictError) Unwrap() error            { return e.APIStatusError }
func (e *GoneError) Unwrap() error                { return e.APIStatusError }
func (e *UnprocessableEntityError) Unwrap() error { return e.APIStatusError }
func (e *TooEarlyError) Unwrap() error            { return e.APIStatusError }
func (e *RateLimitError) Unwrap() error           { return e.APIStatusError }
func (e *ServerError) Unwrap() error              { return e.APIStatusError }

func newStatusError(status *APIStatusError) error {
	if status == nil {
		return nil
	}
	details, traceback := decodeErrorPayload(status.Body)
	if status.Details == nil {
		status.Details = details
	}
	if status.Traceback == nil {
		status.Traceback = traceback
	}
	status.Body = bytes.Clone(status.Body)
	if status.RetryAfter != nil {
		retryAfter := *status.RetryAfter
		status.RetryAfter = &retryAfter
	}

	switch status.StatusCode {
	case 400:
		return &BadRequestError{APIStatusError: status}
	case 401:
		return &AuthenticationError{APIStatusError: status}
	case 403:
		return &PermissionDeniedError{APIStatusError: status}
	case 404:
		return &NotFoundError{APIStatusError: status}
	case 409:
		return &ConflictError{APIStatusError: status}
	case 410:
		return &GoneError{APIStatusError: status}
	case 422:
		return &UnprocessableEntityError{APIStatusError: status}
	case 425:
		return &TooEarlyError{APIStatusError: status}
	case 429:
		return &RateLimitError{APIStatusError: status}
	default:
		if status.StatusCode >= 500 && status.StatusCode < 600 {
			return &ServerError{APIStatusError: status}
		}
		return status
	}
}

func setRetryAfter(status *APIStatusError, delay time.Duration, present bool) {
	if status == nil {
		return
	}
	if !present {
		status.RetryAfter = nil
		return
	}
	status.RetryAfter = &delay
}

func decodeErrorDetails(body []byte) any {
	details, _ := decodeErrorPayload(body)
	return details
}

func decodeErrorPayload(body []byte) (any, []string) {
	if len(body) == 0 {
		return nil, nil
	}
	var payload any
	if err := json.Unmarshal(body, &payload); err != nil {
		return strings.ToValidUTF8(string(body), "�"), nil
	}
	object, ok := payload.(map[string]any)
	if !ok {
		return payload, nil
	}
	details := payload
	if value, exists := object["error"]; exists {
		details = value
	}
	return details, decodeTraceback(object["traceback"])
}

func decodeTraceback(value any) []string {
	items, ok := value.([]any)
	if !ok {
		return nil
	}
	traceback := make([]string, 0, len(items))
	for _, item := range items {
		if text, ok := item.(string); ok {
			traceback = append(traceback, text)
			continue
		}
		encoded, err := json.Marshal(item)
		if err != nil {
			traceback = append(traceback, fmt.Sprint(item))
			continue
		}
		traceback = append(traceback, string(encoded))
	}
	return traceback
}

func errorDetail(details any) string {
	if details == nil {
		return ""
	}
	if text, ok := details.(string); ok {
		return text
	}
	if object, ok := details.(map[string]any); ok {
		for _, key := range []string{"message", "detail", "error"} {
			value, exists := object[key]
			if !exists {
				continue
			}
			if text, ok := value.(string); ok {
				return text
			}
			encoded, err := json.Marshal(value)
			if err == nil {
				return string(encoded)
			}
		}
	}
	encoded, err := json.Marshal(details)
	if err != nil {
		return fmt.Sprint(details)
	}
	return string(encoded)
}

var _ error = (*APIConnectionError)(nil)
var _ interface{ Timeout() bool } = (*APIConnectionError)(nil)
var _ error = (*APIStatusError)(nil)
