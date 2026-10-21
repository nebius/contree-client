package contree

import (
	"bytes"
	"encoding/json"
	"testing"
)

func TestDecodeStreamPayloads(t *testing.T) {
	for _, test := range []struct {
		name string
		data any
		want []byte
		text string
	}{
		{"typed", EventDataStream{Value: "hello", Encoding: "ascii"}, []byte("hello"), "hello"},
		{"pointer", &StreamRepr{Value: "aGk=", Encoding: "base64"}, []byte("hi"), "hi"},
		{"object", map[string]any{"value": "hello"}, []byte("hello"), "hello"},
		{"raw JSON", json.RawMessage(`{"value":"aGk=","encoding":"base64"}`), []byte("hi"), "hi"},
		{"invalid UTF-8", map[string]any{"value": "/w==", "encoding": "base64"}, []byte{255}, "�"},
		{"invalid base64", map[string]any{"value": "!", "encoding": "base64"}, nil, ""},
		{"wrong value", map[string]any{"value": 42}, nil, ""},
		{"unknown encoding", map[string]any{"value": "hi", "encoding": "future"}, []byte("hi"), "hi"},
		{"non-scalar encoding", map[string]any{"value": "hi", "encoding": []any{}}, []byte("hi"), "hi"},
		{"nil", nil, nil, ""},
		{"nil pointer", (*EventDataStream)(nil), nil, ""},
		{"unknown shape", []any{1}, nil, ""},
		{"invalid JSON", json.RawMessage(`{`), nil, ""},
	} {
		t.Run(test.name, func(t *testing.T) {
			if got := DecodeChunk(test.data); !bytes.Equal(got, test.want) {
				t.Fatalf("bytes = %v, want %v", got, test.want)
			}
			if got := DecodeStream(test.data); got != test.text {
				t.Fatalf("text = %q, want %q", got, test.text)
			}
		})
	}
}
