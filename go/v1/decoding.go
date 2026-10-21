package contree

import (
	"encoding/base64"
	"encoding/json"
	"strings"
)

// DecodeChunk decodes a typed stream payload, raw JSON, or a raw object to bytes.
// Unknown shapes and invalid base64 return empty bytes so a live stream can continue.
func DecodeChunk(data any) []byte {
	if isNilJSONValue(data) {
		return []byte{}
	}
	if stream, ok := data.(interface{ AsBytes() []byte }); ok {
		return stream.AsBytes()
	}
	if raw, ok := data.(json.RawMessage); ok {
		var object map[string]any
		if json.Unmarshal(raw, &object) != nil {
			return []byte{}
		}
		data = object
	}
	object, ok := data.(map[string]any)
	if !ok {
		return []byte{}
	}
	value, ok := object["value"].(string)
	if !ok {
		return []byte{}
	}
	if object["encoding"] == "base64" {
		decoded, err := base64.StdEncoding.DecodeString(value)
		if err != nil {
			return []byte{}
		}
		return decoded
	}
	return []byte(strings.ToValidUTF8(value, "�"))
}

// DecodeStream decodes a stream payload to text, replacing invalid UTF-8.
func DecodeStream(data any) string {
	return strings.ToValidUTF8(string(DecodeChunk(data)), "�")
}
