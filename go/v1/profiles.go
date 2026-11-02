package contree

import (
	"bufio"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"

	"github.com/pelletier/go-toml/v2"
)

const (
	// AuthTypeIAM identifies an IAM token profile.
	AuthTypeIAM = "iam"
	// AuthTypeJWT identifies a JWT token profile.
	AuthTypeJWT = "jwt"
	// ProfileSectionPrefix prefixes profile sections in Contree INI files.
	ProfileSectionPrefix = "profile:"
	// DefaultProfileName is selected when the configuration has no active profile.
	DefaultProfileName = "default"
	// DefaultIAMURL is used when an IAM profile does not specify a URL.
	DefaultIAMURL = DefaultBaseURL
)

// Profile contains the settings for one Contree authentication profile.
type Profile struct {
	Name     string
	URL      string
	Token    string
	AuthType string
	Project  string
}

// String returns a profile description without its token.
func (p Profile) String() string {
	return fmt.Sprintf(
		"Profile{Name:%q, URL:%q, AuthType:%q, Project:%q}",
		p.Name,
		p.URL,
		p.AuthType,
		p.Project,
	)
}

// GoString returns a Go-syntax profile description without its token.
func (p Profile) GoString() string { return p.String() }

// ProfileError reports a missing or incomplete profile.
type ProfileError struct {
	Name    string
	Problem string
}

func (e *ProfileError) Error() string {
	if e == nil {
		return "<nil>"
	}
	if e.Name == "" {
		return "contree: profile " + e.Problem
	}
	return fmt.Sprintf("contree: profile %q %s", e.Name, e.Problem)
}

// ContreeHome returns the directory that contains Contree configuration files.
// CONTREE_HOME has priority over XDG_CONFIG_HOME and ~/.config/contree.
func ContreeHome() (string, error) {
	if value := os.Getenv("CONTREE_HOME"); value != "" {
		return expandUserPath(value)
	}
	if value := os.Getenv("XDG_CONFIG_HOME"); value != "" {
		expanded, err := expandUserPath(value)
		if err != nil {
			return "", err
		}
		return filepath.Join(expanded, "contree"), nil
	}
	home, err := os.UserHomeDir()
	if err != nil {
		return "", fmt.Errorf("contree: find user home directory: %w", err)
	}
	return filepath.Join(home, ".config", "contree"), nil
}

// LoadProfiles reads the selected CLI and authentication sources. Each role
// prefers TOML over INI; authentication fields override CLI fields. An explicit
// INI path uses only INI sibling defaults. An explicit TOML path prefers TOML
// sibling defaults. Listing permits incomplete profiles.
func LoadProfiles(path ...string) (map[string]Profile, string, error) {
	profiles, active, _, err := loadProfileData(path...)
	return profiles, active, err
}

func loadProfileData(path ...string) (map[string]Profile, string, bool, error) {
	sources, err := profileSources(path)
	if err != nil {
		return nil, "", false, err
	}
	merged := make(iniSections)
	hasTOML := false
	for _, file := range sources {
		var sections iniSections
		if filepath.Ext(file) == ".toml" {
			hasTOML = true
			sections, err = readProfileTOML(file)
		} else {
			sections, err = readProfileINI(file)
		}
		if err != nil {
			return nil, "", false, err
		}
		mergeINI(merged, sections)
	}

	defaults := merged[""]
	active := DefaultProfileName
	if value, ok := defaults["profile"]; ok {
		active = value
	}

	profiles := make(map[string]Profile)
	for section, values := range merged {
		if !strings.HasPrefix(section, ProfileSectionPrefix) {
			continue
		}
		effective := make(map[string]string, len(defaults)+len(values))
		for key, value := range defaults {
			effective[key] = value
		}
		for key, value := range values {
			effective[key] = value
		}

		authType, hasAuthType := effective["type"]
		if !hasAuthType {
			authType = AuthTypeJWT
		}
		profileURL, hasURL := effective["url"]
		if !hasURL && authType == AuthTypeIAM {
			profileURL = DefaultIAMURL
		}
		name := strings.TrimPrefix(section, ProfileSectionPrefix)
		profiles[name] = Profile{
			Name:     name,
			URL:      strings.TrimRight(profileURL, "/"),
			Token:    effective["token"],
			AuthType: authType,
			Project:  effective["project"],
		}
	}
	return profiles, active, hasTOML, nil
}

// ResolveProfile selects a profile. An explicit name has priority over
// CONTREE_PROFILE and the active profile in the configuration. Pass one path
// to read a specific authentication file.
func ResolveProfile(name string, path ...string) (Profile, error) {
	profiles, active, hasTOML, err := loadProfileData(path...)
	if err != nil {
		return Profile{}, err
	}
	selected := name
	if selected == "" {
		selected = os.Getenv("CONTREE_PROFILE")
	}
	if selected == "" {
		selected = active
	}
	profile, ok := profiles[selected]
	if !ok {
		return Profile{}, &ProfileError{Name: selected, Problem: "not found"}
	}
	if hasTOML {
		if strings.TrimSpace(profile.Token) == "" {
			return Profile{}, &ProfileError{Name: selected, Problem: "has no token"}
		}
		if strings.TrimSpace(profile.URL) == "" && !(profile.AuthType == AuthTypeIAM && profile.URL == "") {
			return Profile{}, &ProfileError{Name: selected, Problem: "has no URL"}
		}
	}
	return profile, nil
}

// ProfileFromEnvironment returns a profile when the environment contains a
// token and URL. CONTREE_* variables have priority over their NEBIUS_* aliases.
func ProfileFromEnvironment() (Profile, bool) {
	token := firstNonEmpty(os.Getenv("CONTREE_TOKEN"), os.Getenv("NEBIUS_API_KEY"))
	profileURL := os.Getenv("CONTREE_URL")
	if token == "" || profileURL == "" {
		return Profile{}, false
	}
	return Profile{
		Name:     "environment",
		URL:      strings.TrimRight(profileURL, "/"),
		Token:    token,
		AuthType: AuthTypeJWT,
		Project: firstNonEmpty(
			os.Getenv("CONTREE_PROJECT"),
			os.Getenv("NEBIUS_AI_PROJECT"),
		),
	}, true
}

// NewClientFromProfile constructs a client from a loaded profile. Resolve and
// construct a saved profile as follows:
//
//	profile, err := ResolveProfile("")
//	if err != nil {
//		return err
//	}
//	client, err := NewClientFromProfile(profile)
//
// ClientOption values are applied after the profile settings. Thus, caller
// options override the profile URL and project.
func NewClientFromProfile(profile Profile, options ...ClientOption) (*Client, error) {
	if strings.TrimSpace(profile.Token) == "" {
		return nil, &ProfileError{Name: profile.Name, Problem: "has no token"}
	}
	if profile.URL == "" && profile.AuthType != AuthTypeIAM {
		return nil, &ProfileError{Name: profile.Name, Problem: "has no URL"}
	}
	profileURL := profile.URL
	if profileURL == "" {
		profileURL = DefaultIAMURL
	}
	profileOptions := make([]ClientOption, 0, len(options)+2)
	profileOptions = append(
		profileOptions,
		WithBaseURL(profileURL),
		WithProject(profile.Project),
	)
	profileOptions = append(profileOptions, options...)
	return NewClient(profile.Token, profileOptions...)
}

type iniSections map[string]map[string]string

func preferredProfileFile(directory, role string) (string, error) {
	candidate := filepath.Join(directory, role+".toml")
	_, err := os.Stat(candidate)
	if errors.Is(err, os.ErrNotExist) {
		return filepath.Join(directory, role+".ini"), nil
	}
	if err != nil {
		return "", fmt.Errorf("contree: inspect profile file %q: %w", candidate, err)
	}
	return candidate, nil
}

func profileSources(path []string) ([]string, error) {
	if len(path) > 1 {
		return nil, errors.New("contree: LoadProfiles accepts at most one path")
	}
	explicit := len(path) == 1 && path[0] != ""
	var auth string
	var err error
	if explicit {
		auth, err = expandUserPath(path[0])
		if err != nil {
			return nil, err
		}
		auth = filepath.Clean(auth)
	} else {
		home, homeErr := ContreeHome()
		if homeErr != nil {
			return nil, homeErr
		}
		auth, err = preferredProfileFile(home, "auth")
		if err != nil {
			return nil, err
		}
	}
	defaults := filepath.Join(filepath.Dir(auth), "cli.ini")
	if !explicit || filepath.Ext(auth) == ".toml" {
		defaults, err = preferredProfileFile(filepath.Dir(auth), "cli")
		if err != nil {
			return nil, err
		}
	}
	if defaults == auth {
		return []string{auth}, nil
	}
	return []string{defaults, auth}, nil
}

func readProfileTOML(path string) (iniSections, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("contree: read profile file %q: %w", path, err)
	}
	var data map[string]any
	if err := toml.Unmarshal(raw, &data); err != nil {
		// Parser errors may include credential lines. Do not wrap them.
		return nil, fmt.Errorf("contree: invalid TOML profile file %q", path)
	}
	sections := iniSections{"": make(map[string]string)}
	if active, present := data["profile"]; present {
		value, ok := active.(string)
		if !ok {
			return nil, fmt.Errorf("contree: invalid profile selector in %q", path)
		}
		sections[""]["profile"] = value
	}
	for name, rawValues := range data {
		if name == "profile" {
			continue
		}
		values, ok := rawValues.(map[string]any)
		if !ok {
			continue
		}
		fields := make(map[string]string)
		for _, key := range []string{"url", "token", "type", "project"} {
			rawValue, present := values[key]
			if !present {
				continue
			}
			value, ok := rawValue.(string)
			if !ok {
				return nil, fmt.Errorf("contree: invalid profile field %q in %q", key, path)
			}
			fields[key] = value
		}
		sections[ProfileSectionPrefix+name] = fields
	}
	return sections, nil
}

func expandUserPath(value string) (string, error) {
	if value != "~" && !strings.HasPrefix(value, "~/") &&
		!(os.PathSeparator == '\\' && strings.HasPrefix(value, `~\`)) {
		return value, nil
	}
	home, err := os.UserHomeDir()
	if err != nil {
		return "", fmt.Errorf("find user home directory: %w", err)
	}
	if value == "~" {
		return home, nil
	}
	remainder := value[2:]
	return filepath.Join(home, filepath.FromSlash(remainder)), nil
}

func readProfileINI(path string) (iniSections, error) {
	file, err := os.Open(path)
	if errors.Is(err, os.ErrNotExist) {
		return make(iniSections), nil
	}
	if err != nil {
		return nil, fmt.Errorf("contree: open profile file %q: %w", path, err)
	}
	defer file.Close()
	sections, err := parseProfileINI(file, path)
	if err != nil {
		return nil, err
	}
	return sections, nil
}

func parseProfileINI(reader io.Reader, path string) (iniSections, error) {
	sections := iniSections{"": make(map[string]string)}
	current := ""
	scanner := bufio.NewScanner(reader)
	scanner.Buffer(make([]byte, 4096), 1024*1024)
	for lineNumber := 1; scanner.Scan(); lineNumber++ {
		line := scanner.Text()
		if lineNumber == 1 {
			line = strings.TrimPrefix(line, "\ufeff")
		}
		line = strings.TrimSpace(line)
		if line == "" || strings.HasPrefix(line, "#") || strings.HasPrefix(line, ";") {
			continue
		}
		if strings.HasPrefix(line, "[") {
			closing := strings.IndexByte(line, ']')
			if closing < 0 {
				return nil, iniSyntaxError(path, lineNumber, "unterminated section header")
			}
			tail := strings.TrimSpace(line[closing+1:])
			if tail != "" && !strings.HasPrefix(tail, "#") &&
				!strings.HasPrefix(tail, ";") {
				return nil, iniSyntaxError(path, lineNumber, "text after section header")
			}
			name := strings.TrimSpace(line[1:closing])
			if name == "" {
				return nil, iniSyntaxError(path, lineNumber, "empty section name")
			}
			if name == "DEFAULT" {
				current = ""
			} else {
				current = name
			}
			if sections[current] == nil {
				sections[current] = make(map[string]string)
			}
			continue
		}

		separator := strings.IndexByte(line, '=')
		if separator < 0 {
			return nil, iniSyntaxError(path, lineNumber, "expected key = value")
		}
		key := strings.ToLower(strings.TrimSpace(line[:separator]))
		if key == "" {
			return nil, iniSyntaxError(path, lineNumber, "empty key")
		}
		sections[current][key] = strings.TrimSpace(line[separator+1:])
	}
	if err := scanner.Err(); err != nil {
		return nil, fmt.Errorf("contree: read profile file %q: %w", path, err)
	}
	return sections, nil
}

func iniSyntaxError(path string, line int, problem string) error {
	return fmt.Errorf(
		"contree: parse profile file %q at line %d: %s",
		path,
		line,
		problem,
	)
}

func mergeINI(target, source iniSections) {
	for section, values := range source {
		if target[section] == nil {
			target[section] = make(map[string]string)
		}
		for key, value := range values {
			target[section][key] = value
		}
	}
}

func firstNonEmpty(values ...string) string {
	for _, value := range values {
		if value != "" {
			return value
		}
	}
	return ""
}
