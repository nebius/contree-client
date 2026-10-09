package contree

import (
	"bytes"
	"compress/gzip"
	"errors"
	"io"
	"net/http"
	"net/url"
	"strings"
	"testing"
	"time"
)

func TestAddQueryValuePreservesRepeatedValues(t *testing.T) {
	t.Parallel()

	query := make(url.Values)
	if err := addQueryValue(query, "pattern", []string{"first", "second"}, true); err != nil {
		t.Fatal(err)
	}
	if got := query["pattern"]; len(got) != 2 || got[0] != "first" || got[1] != "second" {
		t.Fatalf("pattern = %#v", got)
	}
	if err := addQueryValue(query, "pattern", 42, true); err == nil {
		t.Fatal("integer repeatable value succeeded")
	}
}

func TestEncodeQueryPreservesSlashesAndUsesPercentEncodedSpaces(t *testing.T) {
	t.Parallel()

	query := url.Values{
		"filter[]": {"a/b c", "second"},
	}
	got := encodeQuery(query)
	if got != "filter%5B%5D=a/b%20c&filter%5B%5D=second" {
		t.Fatalf("encoded query = %q", got)
	}
}

func TestFormatTimeParam(t *testing.T) {
	t.Parallel()

	instant := time.Date(2026, time.September, 3, 14, 5, 6, 7, time.UTC)
	for _, test := range []struct {
		value any
		want  string
	}{
		{value: "yesterday", want: "yesterday"},
		{value: int64(42), want: "42"},
		{value: 1.25, want: "1.25"},
		{value: instant, want: "2026-09-03T14:05:06.000000007Z"},
	} {
		got, err := formatTimeParam(test.value)
		if err != nil {
			t.Fatalf("formatTimeParam(%#v): %v", test.value, err)
		}
		if got != test.want {
			t.Fatalf("formatTimeParam(%#v) = %q, want %q", test.value, got, test.want)
		}
	}
	if _, err := formatTimeParam(struct{}{}); err == nil {
		t.Fatal("unsupported time parameter succeeded")
	}
}

func TestResponseModesCloseBufferedBodies(t *testing.T) {
	t.Parallel()

	body := &trackingBody{Reader: strings.NewReader(`{"value":17}`)}
	response := responseWithBody(http.StatusOK, body)
	value, err := decodeJSONPathResponse[int64](
		response,
		acceptAny2xx,
		"value",
	)
	if err != nil {
		t.Fatal(err)
	}
	if value != 17 || !body.closed {
		t.Fatalf("value=%d closed=%v", value, body.closed)
	}

	streamBody := &trackingBody{Reader: strings.NewReader("stream")}
	stream, err := streamResponse(
		responseWithBody(http.StatusOK, streamBody),
		acceptAny2xx,
	)
	if err != nil {
		t.Fatal(err)
	}
	if streamBody.closed {
		t.Fatal("stream body closed before caller consumed it")
	}
	_ = stream.Close()
	if !streamBody.closed {
		t.Fatal("stream body did not close")
	}
}

func TestDecodeJSONResponseRejectsNull(t *testing.T) {
	t.Parallel()

	body := &trackingBody{Reader: strings.NewReader(" \n null\t")}
	value, err := decodeJSONResponse[int64](
		responseWithBody(http.StatusOK, body),
		acceptAny2xx,
	)
	if err == nil || !strings.Contains(err.Error(), "must not be null") {
		t.Fatalf("value=%d error=%v", value, err)
	}
	if !body.closed {
		t.Fatal("null response body was not closed")
	}
}

func TestDecodeJSONPathResponseRejectsNull(t *testing.T) {
	t.Parallel()

	for _, test := range []struct {
		name string
		body string
		path []string
	}{
		{name: "top level", body: "null", path: []string{"value"}},
		{name: "first field", body: `{"value":null}`, path: []string{"value"}},
		{
			name: "nested field",
			body: `{"result":{"value":null}}`,
			path: []string{"result", "value"},
		},
		{
			name: "intermediate field",
			body: `{"result":null}`,
			path: []string{"result", "value"},
		},
	} {
		t.Run(test.name, func(t *testing.T) {
			body := &trackingBody{Reader: strings.NewReader(test.body)}
			value, err := decodeJSONPathResponse[string](
				responseWithBody(http.StatusOK, body),
				acceptAny2xx,
				test.path...,
			)
			if err == nil || !strings.Contains(err.Error(), "must not be null") {
				t.Fatalf("value=%q error=%v", value, err)
			}
			if !body.closed {
				t.Fatal("null response body was not closed")
			}
		})
	}
}

func TestStreamResponseDecodesGzipAndRawResponsePreservesIt(t *testing.T) {
	t.Parallel()

	var compressed bytes.Buffer
	writer := gzip.NewWriter(&compressed)
	if _, err := writer.Write([]byte("decoded")); err != nil {
		t.Fatal(err)
	}
	if err := writer.Close(); err != nil {
		t.Fatal(err)
	}

	decodedResponse := responseWithBody(
		http.StatusOK,
		io.NopCloser(bytes.NewReader(compressed.Bytes())),
	)
	decodedResponse.Header.Set("Content-Encoding", "gzip")
	decoded, err := streamResponse(decodedResponse, acceptAny2xx)
	if err != nil {
		t.Fatal(err)
	}
	decodedBytes, err := io.ReadAll(decoded)
	if err != nil {
		t.Fatal(err)
	}
	if err := decoded.Close(); err != nil {
		t.Fatal(err)
	}
	if string(decodedBytes) != "decoded" {
		t.Fatalf("decoded body = %q", decodedBytes)
	}

	rawResponse := responseWithBody(
		http.StatusOK,
		io.NopCloser(bytes.NewReader(compressed.Bytes())),
	)
	rawResponse.Header.Set("Content-Encoding", "gzip")
	raw, err := rawStreamResponse(rawResponse, acceptAny2xx)
	if err != nil {
		t.Fatal(err)
	}
	rawBytes, err := io.ReadAll(raw)
	if err != nil {
		t.Fatal(err)
	}
	_ = raw.Close()
	if !bytes.Equal(rawBytes, compressed.Bytes()) {
		t.Fatal("raw response was decoded")
	}
}

func TestStreamResponseClosesMalformedGzipBody(t *testing.T) {
	t.Parallel()

	body := &trackingBody{Reader: strings.NewReader("not gzip")}
	response := responseWithBody(http.StatusOK, body)
	response.Header.Set("Content-Encoding", "gzip")
	if _, err := streamResponse(response, acceptAny2xx); err == nil {
		t.Fatal("malformed gzip response succeeded")
	}
	if !body.closed {
		t.Fatal("malformed gzip response body was not closed")
	}
	if body.closeCalls != 1 {
		t.Fatalf("malformed gzip response body closed %d times", body.closeCalls)
	}
}

func TestUnexpectedResponseClosesMalformedGzipBodyOnce(t *testing.T) {
	t.Parallel()

	body := &trackingBody{Reader: strings.NewReader("not gzip")}
	response := responseWithBody(http.StatusBadRequest, body)
	response.Header.Set("Content-Encoding", "gzip")
	_ = unexpectedResponse(response)
	if body.closeCalls != 1 {
		t.Fatalf("malformed gzip error body closed %d times", body.closeCalls)
	}
}

func TestBooleanResponseHandlesDocumentedFalseStatus(t *testing.T) {
	t.Parallel()

	value, err := booleanResponse(
		responseWithBody(http.StatusNotFound, io.NopCloser(bytes.NewReader(nil))),
		acceptAny2xx,
		http.StatusNotFound,
	)
	if err != nil || value {
		t.Fatalf("value=%v error=%v", value, err)
	}
}

func TestUnexpectedResponseClassifiesStatus(t *testing.T) {
	t.Parallel()

	request, err := http.NewRequest(http.MethodGet, "https://example.test/v1/item", nil)
	if err != nil {
		t.Fatal(err)
	}
	response := &http.Response{
		StatusCode: http.StatusNotFound,
		Header: http.Header{
			"X-Request-ID": []string{"request-1"},
		},
		Body:    io.NopCloser(strings.NewReader(`{"message":"missing"}`)),
		Request: request,
	}
	err = unexpectedResponse(response)
	var notFound *NotFoundError
	if !errors.As(err, &notFound) {
		t.Fatalf("error = %T, want *NotFoundError", err)
	}
	if notFound.RequestID != "request-1" || !strings.Contains(err.Error(), "missing") {
		t.Fatalf("error = %#v", notFound)
	}
}

func TestLocationResponseReturnsFinalSegment(t *testing.T) {
	t.Parallel()

	response := responseWithBody(http.StatusFound, io.NopCloser(bytes.NewReader(nil)))
	response.Header.Set("Location", "https://example.test/images/image%20id/")
	value, err := locationResponse(
		response,
		acceptStatuses(http.StatusFound),
		"Location",
	)
	if err != nil {
		t.Fatal(err)
	}
	if value != "image id" {
		t.Fatalf("location value = %q", value)
	}
}

func responseWithBody(status int, body io.ReadCloser) *http.Response {
	request, _ := http.NewRequest(http.MethodGet, "https://example.test/v1", nil)
	return &http.Response{
		StatusCode: status,
		Header:     make(http.Header),
		Body:       body,
		Request:    request,
	}
}

type trackingBody struct {
	io.Reader
	closed     bool
	closeCalls int
}

func (b *trackingBody) Close() error {
	b.closed = true
	b.closeCalls++
	return nil
}
