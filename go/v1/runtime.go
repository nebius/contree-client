package contree

import (
	"bytes"
	"compress/gzip"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"path"
	"reflect"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"
)

const (
	apiPathPrefix            = "/v1"
	maxBufferedResponseBytes = 64 << 20
	maxErrorBodyBytes        = 1 << 20
)

type requestSpec struct {
	method      string
	path        string
	query       url.Values
	headers     http.Header
	body        *bodyFactory
	contentType string
	accept      string
	idempotent  bool
}

type bodyFactory struct {
	open       func() (io.ReadCloser, error)
	replayable bool
}

func bytesBody(value []byte) *bodyFactory {
	owned := bytes.Clone(value)
	return &bodyFactory{
		replayable: true,
		open: func() (io.ReadCloser, error) {
			return io.NopCloser(bytes.NewReader(owned)), nil
		},
	}
}

func jsonBody(value any) (*bodyFactory, error) {
	encoded, err := json.Marshal(value)
	if err != nil {
		return nil, fmt.Errorf("contree: encode request body: %w", err)
	}
	return bytesBody(encoded), nil
}

func oneShotBody(value io.Reader) *bodyFactory {
	if isNilReader(value) {
		return &bodyFactory{
			open: func() (io.ReadCloser, error) {
				return nil, errors.New("contree: request body must not be nil")
			},
		}
	}
	if seeker, ok := value.(io.ReadSeeker); ok {
		start, err := seeker.Seek(0, io.SeekCurrent)
		if err == nil {
			return &bodyFactory{
				replayable: true,
				open: func() (io.ReadCloser, error) {
					if _, err := seeker.Seek(start, io.SeekStart); err != nil {
						return nil, fmt.Errorf(
							"contree: rewind request body: %w",
							err,
						)
					}
					return io.NopCloser(seeker), nil
				},
			}
		}
	}
	var mutex sync.Mutex
	used := false
	return &bodyFactory{
		open: func() (io.ReadCloser, error) {
			mutex.Lock()
			defer mutex.Unlock()
			if used {
				return nil, errors.New("contree: request body cannot be replayed")
			}
			used = true
			return io.NopCloser(value), nil
		},
	}
}

func isNilReader(value io.Reader) bool {
	if value == nil {
		return true
	}
	reflected := reflect.ValueOf(value)
	switch reflected.Kind() {
	case reflect.Chan, reflect.Func, reflect.Interface, reflect.Map,
		reflect.Pointer, reflect.Slice:
		return reflected.IsNil()
	default:
		return false
	}
}

type statusPolicy struct {
	any2xx   bool
	statuses map[int]struct{}
}

var acceptAny2xx = statusPolicy{any2xx: true}

func acceptStatuses(statuses ...int) statusPolicy {
	policy := statusPolicy{statuses: make(map[int]struct{}, len(statuses))}
	for _, status := range statuses {
		policy.statuses[status] = struct{}{}
	}
	return policy
}

func (p statusPolicy) accepts(status int) bool {
	if p.any2xx {
		return status >= http.StatusOK && status < http.StatusMultipleChoices
	}
	_, ok := p.statuses[status]
	return ok
}

func decodeJSONResponse[T any](response *http.Response, policy statusPolicy) (T, error) {
	var result T
	body, err := bufferedResponse(response, policy)
	if err != nil {
		return result, err
	}
	if isNullJSONResponse(body) {
		return result, errors.New("contree: JSON response must not be null")
	}
	if err := json.Unmarshal(body, &result); err != nil {
		return result, fmt.Errorf("contree: decode JSON response: %w", err)
	}
	return result, nil
}

func decodeJSONPathResponse[T any](
	response *http.Response,
	policy statusPolicy,
	jsonPath ...string,
) (T, error) {
	var result T
	body, err := bufferedResponse(response, policy)
	if err != nil {
		return result, err
	}
	if isNullJSONResponse(body) {
		return result, errors.New("contree: JSON response must not be null")
	}
	value := json.RawMessage(body)
	for index, name := range jsonPath {
		var object map[string]json.RawMessage
		if err := json.Unmarshal(value, &object); err != nil {
			return result, fmt.Errorf("contree: decode JSON response object: %w", err)
		}
		next, ok := object[name]
		if !ok {
			return result, fmt.Errorf("contree: JSON response has no %q field", name)
		}
		if isNullJSONResponse(next) {
			return result, fmt.Errorf(
				"contree: JSON response field %q must not be null",
				strings.Join(jsonPath[:index+1], "."),
			)
		}
		value = next
	}
	if err := json.Unmarshal(value, &result); err != nil {
		return result, fmt.Errorf("contree: decode JSON response value: %w", err)
	}
	return result, nil
}

func isNullJSONResponse(value []byte) bool {
	return bytes.Equal(bytes.TrimSpace(value), []byte("null"))
}

func emptyResponse(response *http.Response, policy statusPolicy) error {
	_, err := bufferedResponse(response, policy)
	return err
}

func booleanResponse(
	response *http.Response,
	policy statusPolicy,
	falseStatuses ...int,
) (bool, error) {
	if policy.accepts(response.StatusCode) {
		_, err := bufferedResponse(response, policy)
		return err == nil, err
	}
	for _, status := range falseStatuses {
		if response.StatusCode == status {
			drainAndClose(response.Body)
			return false, nil
		}
	}
	return false, unexpectedResponse(response)
}

func byteResponse(response *http.Response, policy statusPolicy) ([]byte, error) {
	return bufferedResponse(response, policy)
}

func streamResponse(response *http.Response, policy statusPolicy) (io.ReadCloser, error) {
	if !policy.accepts(response.StatusCode) {
		return nil, unexpectedResponse(response)
	}
	return decodedResponseBody(response)
}

func rawStreamResponse(response *http.Response, policy statusPolicy) (io.ReadCloser, error) {
	if !policy.accepts(response.StatusCode) {
		return nil, unexpectedResponse(response)
	}
	return response.Body, nil
}

func locationResponse(
	response *http.Response,
	policy statusPolicy,
	headerName string,
) (string, error) {
	if !policy.accepts(response.StatusCode) {
		return "", unexpectedResponse(response)
	}
	defer drainAndClose(response.Body)
	location := response.Header.Get(headerName)
	if location == "" {
		return "", fmt.Errorf("contree: response has no %s header", headerName)
	}
	parsed, err := url.Parse(location)
	if err != nil {
		return "", fmt.Errorf("contree: parse response location: %w", err)
	}
	value := path.Base(strings.TrimRight(parsed.Path, "/"))
	if value == "." || value == "/" || value == "" {
		return "", fmt.Errorf("contree: response location has no final path segment")
	}
	decoded, err := url.PathUnescape(value)
	if err != nil {
		return "", fmt.Errorf("contree: decode response location: %w", err)
	}
	return decoded, nil
}

func bufferedResponse(response *http.Response, policy statusPolicy) ([]byte, error) {
	body, err := streamResponse(response, policy)
	if err != nil {
		return nil, err
	}
	defer body.Close()
	data, err := readLimited(body, maxBufferedResponseBytes)
	if err != nil {
		return nil, fmt.Errorf("contree: read response body: %w", err)
	}
	return data, nil
}

func unexpectedResponse(response *http.Response) error {
	bodyReader, decodeErr := decodedResponseBody(response)
	if decodeErr != nil {
		bodyReader = io.NopCloser(bytes.NewReader(nil))
	}
	defer bodyReader.Close()
	body, readErr := readLimited(bodyReader, maxErrorBodyBytes)
	if readErr != nil {
		body = nil
	}
	status := &APIStatusError{
		StatusCode: response.StatusCode,
		Method:     response.Request.Method,
		URL:        response.Request.URL.String(),
		RequestID:  requestID(response.Header),
		Body:       body,
		Details:    decodeErrorDetails(body),
	}
	retryAfter, present := parseRetryAfter(
		response.Header.Get("Retry-After"),
		time.Now(),
	)
	setRetryAfter(status, retryAfter, present)
	return newStatusError(status)
}

func decodedResponseBody(response *http.Response) (io.ReadCloser, error) {
	if response == nil || response.Body == nil {
		return nil, errors.New("contree: response has no body")
	}
	if response.Uncompressed || !strings.EqualFold(
		strings.TrimSpace(response.Header.Get("Content-Encoding")),
		"gzip",
	) {
		return response.Body, nil
	}
	reader, err := gzip.NewReader(response.Body)
	if err != nil {
		return nil, errors.Join(
			fmt.Errorf("contree: decode gzip response: %w", err),
			response.Body.Close(),
		)
	}
	return &gzipResponseBody{Reader: reader, source: response.Body}, nil
}

type gzipResponseBody struct {
	*gzip.Reader
	source io.ReadCloser
}

func (b *gzipResponseBody) Close() error {
	return errors.Join(b.Reader.Close(), b.source.Close())
}

func readLimited(reader io.Reader, maximum int64) ([]byte, error) {
	limited := io.LimitReader(reader, maximum+1)
	body, err := io.ReadAll(limited)
	if err != nil {
		return nil, err
	}
	if int64(len(body)) > maximum {
		return nil, fmt.Errorf("response body exceeds %d bytes", maximum)
	}
	return body, nil
}

func drainAndClose(body io.ReadCloser) {
	_, _ = io.Copy(io.Discard, io.LimitReader(body, 64<<10))
	_ = body.Close()
}

func requestID(header http.Header) string {
	for _, wanted := range []string{"X-Request-ID", "Request-ID", "Traceparent"} {
		for name, values := range header {
			if strings.EqualFold(name, wanted) && len(values) > 0 && values[0] != "" {
				return values[0]
			}
		}
	}
	return ""
}

func quotePath(value any) string {
	return url.PathEscape(fmt.Sprint(value))
}

func addQueryValue(values url.Values, name string, value any, repeatable bool) error {
	if repeatable {
		switch items := value.(type) {
		case string:
			values.Add(name, items)
			return nil
		case []string:
			for _, item := range items {
				values.Add(name, item)
			}
			return nil
		default:
			return fmt.Errorf(
				"contree: query parameter %s requires string or []string, got %T",
				name,
				value,
			)
		}
	}
	values.Add(name, fmt.Sprint(value))
	return nil
}

func encodeQuery(values url.Values) string {
	if len(values) == 0 {
		return ""
	}
	keys := make([]string, 0, len(values))
	for key := range values {
		keys = append(keys, key)
	}
	sort.Strings(keys)
	parts := make([]string, 0, len(values))
	for _, key := range keys {
		for _, value := range values[key] {
			parts = append(parts, escapeQueryPart(key)+"="+escapeQueryPart(value))
		}
	}
	return strings.Join(parts, "&")
}

func escapeQueryPart(value string) string {
	escaped := url.QueryEscape(value)
	escaped = strings.ReplaceAll(escaped, "+", "%20")
	return strings.ReplaceAll(escaped, "%2F", "/")
}

func formatTimeParam(value any) (string, error) {
	switch item := value.(type) {
	case time.Time:
		return item.Format(time.RFC3339Nano), nil
	case string:
		return item, nil
	case int:
		return strconv.Itoa(item), nil
	case int8:
		return strconv.FormatInt(int64(item), 10), nil
	case int16:
		return strconv.FormatInt(int64(item), 10), nil
	case int32:
		return strconv.FormatInt(int64(item), 10), nil
	case int64:
		return strconv.FormatInt(item, 10), nil
	case uint:
		return strconv.FormatUint(uint64(item), 10), nil
	case uint8:
		return strconv.FormatUint(uint64(item), 10), nil
	case uint16:
		return strconv.FormatUint(uint64(item), 10), nil
	case uint32:
		return strconv.FormatUint(uint64(item), 10), nil
	case uint64:
		return strconv.FormatUint(item, 10), nil
	case float32:
		return strconv.FormatFloat(float64(item), 'g', -1, 32), nil
	case float64:
		return strconv.FormatFloat(item, 'g', -1, 64), nil
	default:
		return "", fmt.Errorf(
			"contree: time parameter requires string, number, or time.Time, got %T",
			value,
		)
	}
}
