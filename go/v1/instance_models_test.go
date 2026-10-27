package contree

import (
	"encoding/json"
	"reflect"
	"testing"
)

func TestLiveInstanceEventPayloads(t *testing.T) {
	for _, tc := range []struct {
		name     string
		kind     string
		payload  string
		expected any
	}{
		{"inspect started", "inspect", `{"request_id":"rpc-1","method":"upload","phase":"started","params":{"path":"/workspace","file_uuid":"file-1","unpack":true,"replace":true,"merge":false}}`, EventDataInspect{}},
		{"inspect completed", "inspect", `{"request_id":"rpc-1","method":"upload","phase":"completed","status":"success","error":null,"handle":"upload-1"}`, EventDataInspect{}},
		{"file upload", "file_upload", `{"handle":"upload-1","file_uuid":"file-1","path":"/workspace","unpack":true,"replace":true,"merge":false,"bytes":512,"status":"success","error":null}`, EventDataFileUpload{}},
		{"incomplete upload", "file_upload", `{"handle":"incomplete"}`, map[string]any{}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			var event OperationEvent
			wire := `{"id":1,"ts":"2026-10-27T12:00:00Z","spid":0,"type":"` + tc.kind + `","data":` + tc.payload + `}`
			if err := json.Unmarshal([]byte(wire), &event); err != nil {
				t.Fatal(err)
			}

			if reflect.TypeOf(event.Data) != reflect.TypeOf(tc.expected) {
				t.Fatalf("payload type = %T, want %T", event.Data, tc.expected)
			}
			encoded, err := json.Marshal(event.Data)
			if err != nil {
				t.Fatal(err)
			}
			assertInstanceJSONEqual(t, encoded, []byte(tc.payload))
		})
	}
}

func TestProcessResourceLimitsRoundtrip(t *testing.T) {
	for _, limits := range []string{`{}`, `{"cpu":null,"memory_mib":null}`, `{"cpu":1,"memory_mib":256}`} {
		t.Run(limits, func(t *testing.T) {
			wire := []byte(`{"command":"/bin/true","resources_limits":` + limits + `}`)
			var model ExecSpec
			if err := json.Unmarshal(wire, &model); err != nil {
				t.Fatal(err)
			}

			encoded, err := json.Marshal(model)
			if err != nil {
				t.Fatal(err)
			}
			assertInstanceJSONEqual(t, encoded, wire)
		})
	}
}

func assertInstanceJSONEqual(t *testing.T, got, want []byte) {
	t.Helper()
	var gotValue, wantValue any
	if err := json.Unmarshal(got, &gotValue); err != nil {
		t.Fatal(err)
	}
	if err := json.Unmarshal(want, &wantValue); err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(gotValue, wantValue) {
		t.Fatalf("JSON = %s, want %s", got, want)
	}
}
