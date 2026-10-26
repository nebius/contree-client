package contree_test

import (
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
)

func TestMockResultTypes(t *testing.T) {
	module, err := filepath.Abs("..")
	if err != nil {
		t.Fatal(err)
	}
	directory := t.TempDir()
	mod := "module mocktypes.invalid\n\ngo 1.27\n\nrequire github.com/nebius/contree-client/go v0.0.0\nreplace github.com/nebius/contree-client/go => " + strconv.Quote(module) + "\n"
	if err := os.WriteFile(filepath.Join(directory, "go.mod"), []byte(mod), 0o600); err != nil {
		t.Fatal(err)
	}
	for _, test := range []struct {
		name  string
		call  string
		valid bool
	}{
		{"model", "m.Mock(contree.OpWhoAmI, contree.WhoAmIResponse{})", true},
		{"wrong model", "m.Mock(contree.OpWhoAmI, contree.Image{})", false},
		{"descriptor conversion", "m.Mock(contree.Operation[contree.Image](contree.OpWhoAmI), contree.Image{})", false},
		{"explicit any", "m.Mock[any](contree.OpWhoAmI, contree.Image{})", false},
		{"wrong boolean", `m.Mock(contree.OpCheckFileExists, "false")`, false},
		{"wrong empty", "m.Mock(contree.OpCancelOperation, true)", false},
		{"wrong bytes", `m.Mock(contree.OpInspectImageArchive, "data")`, false},
		{"wrong event", "m.Mock(contree.OpIterOperationEvents, []byte{})", false},
		{"wrong nested result", `m.Mock(contree.OpOperationSubprocessCreate, "42")`, false},
		{"wrong stream", "m.MockStream(contree.OpIterOperationEvents, []byte{}, nil)", false},
	} {
		t.Run(test.name, func(t *testing.T) {
			source := "package mocktypes\nimport contree \"github.com/nebius/contree-client/go/v1\"\nfunc configure(m *contree.MockTransport) error { return " + test.call + " }\n"
			if err := os.WriteFile(filepath.Join(directory, "mock.go"), []byte(source), 0o600); err != nil {
				t.Fatal(err)
			}
			command := exec.Command("go", "build", "./...")
			command.Dir = directory
			command.Env = append(os.Environ(), "GOWORK=off", "GOTOOLCHAIN=local")
			output, err := command.CombinedOutput()
			if test.valid && err != nil {
				t.Fatalf("valid mock does not compile: %v\n%s", err, output)
			}
			if !test.valid && (err == nil || !strings.Contains(string(output), "mock.go:")) {
				t.Fatalf("expected a mock type error, got %v\n%s", err, output)
			}
		})
	}
}
