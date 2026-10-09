package contree

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
)

func TestLoadProfilesMergesFilesAndInheritsDefaults(t *testing.T) {
	t.Parallel()

	home := t.TempDir()
	authPath := filepath.Join(home, "auth.ini")
	writeProfileFile(t, filepath.Join(home, "cli.ini"), `
[DEFAULT]
profile = staging
project = shared-project

[profile:default]
token = stale-token
url = https://prod.example/

[profile:staging]
url = https://staging.example/

[unrelated]
token = ignored
`)
	writeProfileFile(t, authPath, `
[DEFAULT]
project = auth-project

[profile:default]
token = fresh-token

[profile:staging]
token = staging-token
`)

	profiles, active, err := LoadProfiles(authPath)
	if err != nil {
		t.Fatal(err)
	}
	if active != "staging" {
		t.Fatalf("active = %q", active)
	}
	if len(profiles) != 2 {
		t.Fatalf("profiles = %#v", profiles)
	}
	production := profiles["default"]
	if production.Token != "fresh-token" {
		t.Fatalf("default token = %q", production.Token)
	}
	if production.URL != "https://prod.example" {
		t.Fatalf("default URL = %q", production.URL)
	}
	if production.Project != "auth-project" {
		t.Fatalf("default project = %q", production.Project)
	}
	if production.AuthType != AuthTypeJWT {
		t.Fatalf("default auth type = %q", production.AuthType)
	}
	staging := profiles["staging"]
	if staging.Token != "staging-token" || staging.Project != "auth-project" {
		t.Fatalf("staging = %#v", staging)
	}
}

func TestLoadProfilesUsesIAMDefaultURL(t *testing.T) {
	t.Parallel()

	authPath := filepath.Join(t.TempDir(), "auth.ini")
	writeProfileFile(t, authPath, `
[profile:iam]
token = iam-token
type = iam

[profile:legacy]
token = jwt-token
url = https://legacy.example///

[profile:empty-type]
token = empty-token
type =
url = https://empty.example
`)
	profiles, active, err := LoadProfiles(authPath)
	if err != nil {
		t.Fatal(err)
	}
	if active != DefaultProfileName {
		t.Fatalf("active = %q", active)
	}
	if profiles["iam"].URL != DefaultIAMURL {
		t.Fatalf("IAM URL = %q", profiles["iam"].URL)
	}
	if profiles["legacy"].AuthType != AuthTypeJWT {
		t.Fatalf("legacy auth type = %q", profiles["legacy"].AuthType)
	}
	if profiles["legacy"].URL != "https://legacy.example" {
		t.Fatalf("legacy URL = %q", profiles["legacy"].URL)
	}
	if profiles["empty-type"].AuthType != "" {
		t.Fatalf("explicit empty auth type = %q", profiles["empty-type"].AuthType)
	}
}

func TestResolveProfilePriority(t *testing.T) {
	authPath := filepath.Join(t.TempDir(), "auth.ini")
	writeProfileFile(t, authPath, `
[DEFAULT]
profile = active
[profile:active]
token = active-token
url = https://active.example
[profile:environment]
token = environment-token
url = https://environment.example
[profile:explicit]
token = explicit-token
url = https://explicit.example
`)
	t.Setenv("CONTREE_PROFILE", "environment")

	resolved, err := ResolveProfile("", authPath)
	if err != nil {
		t.Fatal(err)
	}
	if resolved.Name != "environment" {
		t.Fatalf("environment selection = %q", resolved.Name)
	}
	resolved, err = ResolveProfile("explicit", authPath)
	if err != nil {
		t.Fatal(err)
	}
	if resolved.Name != "explicit" {
		t.Fatalf("explicit selection = %q", resolved.Name)
	}

	t.Setenv("CONTREE_PROFILE", "")
	resolved, err = ResolveProfile("", authPath)
	if err != nil {
		t.Fatal(err)
	}
	if resolved.Name != "active" {
		t.Fatalf("active selection = %q", resolved.Name)
	}
}

func TestResolveProfileReportsMissingProfile(t *testing.T) {
	t.Parallel()

	authPath := filepath.Join(t.TempDir(), "auth.ini")
	writeProfileFile(t, authPath, "")
	_, err := ResolveProfile("missing", authPath)
	var profileErr *ProfileError
	if !errors.As(err, &profileErr) {
		t.Fatalf("error = %T %v", err, err)
	}
	if profileErr.Name != "missing" || !strings.Contains(err.Error(), "not found") {
		t.Fatalf("error = %#v", profileErr)
	}
}

func TestLoadProfilesRejectsMalformedINI(t *testing.T) {
	t.Parallel()

	tests := map[string]string{
		"unterminated section":  "[profile:default\ntoken = value\n",
		"trailing section text": "[profile:default] invalid\ntoken = value\n",
		"empty section":         "[]\ntoken = value\n",
		"missing separator":     "[profile:default]\ntoken value\n",
		"empty key":             "[profile:default]\n = value\n",
	}
	for name, contents := range tests {
		name, contents := name, contents
		t.Run(name, func(t *testing.T) {
			t.Parallel()
			authPath := filepath.Join(t.TempDir(), "auth.ini")
			writeProfileFile(t, authPath, contents)
			_, _, err := LoadProfiles(authPath)
			if err == nil || !strings.Contains(err.Error(), "line") {
				t.Fatalf("error = %v", err)
			}
		})
	}
}

func TestLoadProfilesSurfacesUnreadableFiles(t *testing.T) {
	t.Parallel()

	authPath := filepath.Join(t.TempDir(), "auth.ini")
	if err := os.Mkdir(authPath, 0o700); err != nil {
		t.Fatal(err)
	}
	_, _, err := LoadProfiles(authPath)
	if err == nil {
		t.Fatal("LoadProfiles accepted a directory as auth.ini")
	}
}

func TestLoadProfilesAllowsMissingFiles(t *testing.T) {
	t.Parallel()

	profiles, active, err := LoadProfiles(filepath.Join(t.TempDir(), "auth.ini"))
	if err != nil {
		t.Fatal(err)
	}
	if len(profiles) != 0 || active != DefaultProfileName {
		t.Fatalf("profiles=%#v active=%q", profiles, active)
	}
}

func TestProfileFromEnvironment(t *testing.T) {
	for _, name := range []string{
		"CONTREE_TOKEN",
		"NEBIUS_API_KEY",
		"CONTREE_URL",
		"CONTREE_PROJECT",
		"NEBIUS_AI_PROJECT",
	} {
		t.Setenv(name, "")
	}
	if profile, ok := ProfileFromEnvironment(); ok {
		t.Fatalf("unexpected profile: %#v", profile)
	}

	t.Setenv("NEBIUS_API_KEY", "fallback-token")
	t.Setenv("CONTREE_URL", "https://environment.example///")
	t.Setenv("NEBIUS_AI_PROJECT", "fallback-project")
	profile, ok := ProfileFromEnvironment()
	if !ok {
		t.Fatal("ProfileFromEnvironment did not find fallback variables")
	}
	if profile.Token != "fallback-token" ||
		profile.URL != "https://environment.example" ||
		profile.Project != "fallback-project" {
		t.Fatalf("fallback profile = %#v", profile)
	}

	t.Setenv("CONTREE_TOKEN", "contree-token")
	t.Setenv("CONTREE_PROJECT", "contree-project")
	profile, ok = ProfileFromEnvironment()
	if !ok || profile.Token != "contree-token" || profile.Project != "contree-project" {
		t.Fatalf("Contree profile = %#v, %t", profile, ok)
	}
}

func TestContreeHomePrecedenceAndExpansion(t *testing.T) {
	home := t.TempDir()
	setProfileTestHome(t, home)
	t.Setenv("CONTREE_HOME", "~/custom")
	t.Setenv("XDG_CONFIG_HOME", filepath.Join(home, "xdg"))
	resolved, err := ContreeHome()
	if err != nil {
		t.Fatal(err)
	}
	if resolved != filepath.Join(home, "custom") {
		t.Fatalf("CONTREE_HOME = %q", resolved)
	}

	t.Setenv("CONTREE_HOME", "")
	t.Setenv("XDG_CONFIG_HOME", "~/xdg")
	resolved, err = ContreeHome()
	if err != nil {
		t.Fatal(err)
	}
	if resolved != filepath.Join(home, "xdg", "contree") {
		t.Fatalf("XDG_CONFIG_HOME = %q", resolved)
	}

	t.Setenv("XDG_CONFIG_HOME", "")
	resolved, err = ContreeHome()
	if err != nil {
		t.Fatal(err)
	}
	if resolved != filepath.Join(home, ".config", "contree") {
		t.Fatalf("default config directory = %q", resolved)
	}
}

func TestLoadProfilesExpandsExplicitPath(t *testing.T) {
	home := t.TempDir()
	setProfileTestHome(t, home)
	authPath := filepath.Join(home, "credentials", "auth.ini")
	if err := os.MkdirAll(filepath.Dir(authPath), 0o700); err != nil {
		t.Fatal(err)
	}
	writeProfileFile(t, authPath, "[profile:default]\ntoken=t\ntype=iam\n")
	profiles, _, err := LoadProfiles("~/credentials/auth.ini")
	if err != nil {
		t.Fatal(err)
	}
	if profiles["default"].Token != "t" {
		t.Fatalf("profiles = %#v", profiles)
	}
}

func TestNewClientFromProfileAndOptionPrecedence(t *testing.T) {
	t.Parallel()

	client, err := NewClientFromProfile(
		Profile{
			Name:     "saved",
			URL:      "https://profile.example/",
			Token:    "secret-token",
			AuthType: AuthTypeJWT,
			Project:  "profile-project",
		},
		WithBaseURL("https://caller.example/"),
		WithProject("caller-project"),
	)
	if err != nil {
		t.Fatal(err)
	}
	if client.token != "secret-token" ||
		client.baseURL != "https://caller.example" ||
		client.project != "caller-project" {
		t.Fatalf("client = %#v", client)
	}

	iamClient, err := NewClientFromProfile(Profile{
		Name:     "iam",
		Token:    "iam-token",
		AuthType: AuthTypeIAM,
	})
	if err != nil {
		t.Fatal(err)
	}
	if iamClient.baseURL != DefaultIAMURL {
		t.Fatalf("IAM base URL = %q", iamClient.baseURL)
	}
}

func TestNewClientFromProfileRejectsIncompleteProfiles(t *testing.T) {
	t.Parallel()

	tests := []Profile{
		{Name: "missing-token", URL: "https://example.test", AuthType: AuthTypeJWT},
		{Name: "blank-token", URL: "https://example.test", Token: "  ", AuthType: AuthTypeJWT},
		{Name: "missing-url", Token: "token", AuthType: AuthTypeJWT},
	}
	for _, profile := range tests {
		profile := profile
		t.Run(profile.Name, func(t *testing.T) {
			t.Parallel()
			_, err := NewClientFromProfile(profile)
			var profileErr *ProfileError
			if !errors.As(err, &profileErr) {
				t.Fatalf("error = %T %v", err, err)
			}
		})
	}
}

func TestProfileFormattingHidesToken(t *testing.T) {
	t.Parallel()

	profile := Profile{
		Name:     "private",
		URL:      "https://example.test",
		Token:    "do-not-print",
		AuthType: AuthTypeJWT,
		Project:  "project",
	}
	for _, format := range []string{"%s", "%v", "%+v", "%#v"} {
		if rendered := fmt.Sprintf(format, profile); strings.Contains(rendered, profile.Token) {
			t.Fatalf("format %q exposed the token: %s", format, rendered)
		}
	}
}

func TestLoadProfilesRejectsMultiplePaths(t *testing.T) {
	t.Parallel()

	if _, _, err := LoadProfiles("first", "second"); err == nil {
		t.Fatal("LoadProfiles accepted multiple paths")
	}
}

func writeProfileFile(t *testing.T, path, contents string) {
	t.Helper()
	if err := os.WriteFile(path, []byte(contents), 0o600); err != nil {
		t.Fatal(err)
	}
}

// os.UserHomeDir uses USERPROFILE on Windows and HOME on Unix.
func setProfileTestHome(t *testing.T, home string) {
	t.Helper()
	variable := "HOME"
	if runtime.GOOS == "windows" {
		variable = "USERPROFILE"
	}
	t.Setenv(variable, home)
}
