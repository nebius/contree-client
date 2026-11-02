package contree

import (
	"errors"
	"fmt"
	"path/filepath"
	"strings"
	"testing"
)

func TestTOMLPartialMergeAndSafeFailure(t *testing.T) {
	t.Parallel()
	home := t.TempDir()
	writeProfileFile(t, filepath.Join(home, "cli.ini"), "[DEFAULT]\nprofile = dev\n[profile:dev]\nurl = https://example.test/\nproject = cli-project\n")
	writeProfileFile(t, filepath.Join(home, "auth.ini"), "[profile:ignored]\ntoken = ignored-token\nurl = https://ignored.test\n")
	path := filepath.Join(home, "e2e.toml")
	writeProfileFile(t, path, "[dev]\ntoken = \"dummy%token\"\n[partial]\n")
	profiles, active, err := LoadProfiles(path)
	if err != nil {
		t.Fatal(err)
	}
	if active != "dev" || len(profiles) != 2 {
		t.Fatalf("unexpected profiles: %v, active %q", profiles, active)
	}
	profile, err := ResolveProfile("dev", path)
	if err != nil {
		t.Fatal(err)
	}
	if profile.Token != "dummy%token" || profile.URL != "https://example.test" || profile.Project != "cli-project" {
		t.Fatalf("unexpected profile: %v", profile)
	}
	_, err = ResolveProfile("partial", path)
	var profileError *ProfileError
	if !errors.As(err, &profileError) {
		t.Fatalf("expected incomplete profile error: %v", err)
	}
	writeProfileFile(t, path, "[dev]\ntoken = \"SECRET-MARKER\" broken")
	_, _, err = LoadProfiles(path)
	if err == nil || !strings.Contains(err.Error(), fmt.Sprintf("%q", path)) || strings.Contains(fmt.Sprintf("%+v", err), "SECRET-MARKER") {
		t.Fatalf("expected safe source error: %v", err)
	}
	if _, err := ResolveProfile("dev", filepath.Join(home, "missing.toml")); err == nil {
		t.Fatal("missing TOML must fail")
	}
}
