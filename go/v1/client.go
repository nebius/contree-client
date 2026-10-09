package contree

import (
	"context"
	"errors"
	"fmt"
	"io"
	"math"
	"net"
	"net/http"
	"net/url"
	"runtime"
	"runtime/debug"
	"strconv"
	"strings"
	"time"
)

const defaultRequestTimeout = 300 * time.Second

const modulePath = "github.com/nebius/contree-client/go"

var defaultRetryDelays = []time.Duration{
	100 * time.Millisecond,
	200 * time.Millisecond,
	500 * time.Millisecond,
	time.Second,
	2 * time.Second,
	5 * time.Second,
}

// RetryPolicy controls retries for idempotent buffered requests. MaxAttempts
// includes the initial request. A value of one disables retries.
type RetryPolicy struct {
	Statuses     []int
	ServerErrors bool
	Delays       []time.Duration
	MaxAttempts  int
	RetryUnsafe  bool
}

// DefaultRetryPolicy returns a conservative exponential retry policy.
func DefaultRetryPolicy() RetryPolicy {
	return RetryPolicy{
		Statuses:     []int{410, 425, http.StatusTooManyRequests},
		ServerErrors: true,
		Delays:       append([]time.Duration(nil), defaultRetryDelays...),
		MaxAttempts:  10,
	}
}

func (p RetryPolicy) validate() error {
	if p.MaxAttempts < 1 {
		return errors.New("contree: retry MaxAttempts must be at least 1")
	}
	if len(p.Delays) == 0 {
		return errors.New("contree: retry Delays must not be empty")
	}
	for _, delay := range p.Delays {
		if delay < 0 {
			return errors.New("contree: retry delays must not be negative")
		}
	}
	for _, status := range p.Statuses {
		if status < 100 || status > 599 {
			return fmt.Errorf("contree: invalid retry HTTP status %d", status)
		}
	}
	return nil
}

type clientConfig struct {
	baseURL   string
	project   string
	timeout   time.Duration
	retry     *RetryPolicy
	http      *http.Client
	identity  string
	userAgent string
}

// ClientOption changes one Client setting.
type ClientOption func(*clientConfig) error

// WithBaseURL overrides the service origin. The client appends /v1 and the
// generated operation path.
func WithBaseURL(value string) ClientOption {
	return func(config *clientConfig) error {
		config.baseURL = value
		return nil
	}
}

// WithProject sets the Project request header. An empty value omits it.
func WithProject(value string) ClientOption {
	return func(config *clientConfig) error {
		config.project = value
		return nil
	}
}

// WithTimeout sets the whole-request deadline for buffered operations,
// including retries and response reads. Caller-owned streams use their context.
// A zero value disables the client deadline.
func WithTimeout(value time.Duration) ClientOption {
	return func(config *clientConfig) error {
		if value < 0 {
			return errors.New("contree: timeout must not be negative")
		}
		config.timeout = value
		return nil
	}
}

// WithRetry enables retries for idempotent buffered requests.
func WithRetry(policy RetryPolicy) ClientOption {
	return func(config *clientConfig) error {
		if err := policy.validate(); err != nil {
			return err
		}
		policy.Statuses = append([]int(nil), policy.Statuses...)
		policy.Delays = append([]time.Duration(nil), policy.Delays...)
		config.retry = &policy
		return nil
	}
}

// WithoutRetry disables retries. This is the default.
func WithoutRetry() ClientOption {
	return func(config *clientConfig) error {
		config.retry = nil
		return nil
	}
}

// WithHTTPClient supplies the transport settings used by the client. NewClient
// copies the value and disables automatic redirects so generated 3xx responses
// remain observable. Callers retain ownership of the supplied client.
func WithHTTPClient(value *http.Client) ClientOption {
	return func(config *clientConfig) error {
		if value == nil {
			return errors.New("contree: HTTP client must not be nil")
		}
		config.http = value
		return nil
	}
}

// WithIdentity prepends an application product token to the User-Agent.
func WithIdentity(value string) ClientOption {
	return func(config *clientConfig) error {
		if strings.ContainsAny(value, "\r\n") {
			return errors.New("contree: identity must not contain a newline")
		}
		config.identity = strings.TrimSpace(value)
		return nil
	}
}

// WithUserAgent replaces the complete generated User-Agent value.
func WithUserAgent(value string) ClientOption {
	return func(config *clientConfig) error {
		if strings.ContainsAny(value, "\r\n") {
			return errors.New("contree: User-Agent must not contain a newline")
		}
		config.userAgent = strings.TrimSpace(value)
		return nil
	}
}

// Client calls the generated Contree API surface through net/http.
type Client struct {
	token     string
	baseURL   string
	project   string
	timeout   time.Duration
	retry     *RetryPolicy
	http      *http.Client
	userAgent string
}

// NewClient constructs a client. An empty token omits Authorization for public
// endpoints.
func NewClient(token string, options ...ClientOption) (*Client, error) {
	config := clientConfig{
		baseURL: DefaultBaseURL,
		timeout: defaultRequestTimeout,
		http:    http.DefaultClient,
	}
	for _, option := range options {
		if option == nil {
			return nil, errors.New("contree: ClientOption must not be nil")
		}
		if err := option(&config); err != nil {
			return nil, err
		}
	}
	baseURL, err := validateBaseURL(config.baseURL)
	if err != nil {
		return nil, err
	}
	httpClient := *config.http
	httpClient.CheckRedirect = func(_ *http.Request, _ []*http.Request) error {
		return http.ErrUseLastResponse
	}
	userAgent := config.userAgent
	if userAgent == "" {
		userAgent = defaultUserAgent(config.identity)
	}
	return &Client{
		token:     token,
		baseURL:   baseURL,
		project:   config.project,
		timeout:   config.timeout,
		retry:     config.retry,
		http:      &httpClient,
		userAgent: userAgent,
	}, nil
}

// UserAgent returns the value sent by this client.
func (c *Client) UserAgent() string { return c.userAgent }

// BaseURL returns the normalized service origin.
func (c *Client) BaseURL() string { return c.baseURL }

func validateBaseURL(value string) (string, error) {
	parsed, err := url.Parse(value)
	if err != nil {
		return "", fmt.Errorf("contree: invalid base URL %q: %w", value, err)
	}
	if parsed.Scheme != "http" && parsed.Scheme != "https" {
		return "", fmt.Errorf(
			"contree: unsupported base URL scheme %q: use http or https",
			parsed.Scheme,
		)
	}
	if parsed.Hostname() == "" {
		return "", fmt.Errorf("contree: base URL %q has no hostname", value)
	}
	if strings.ContainsAny(parsed.Hostname(), " \t\r\n") {
		return "", fmt.Errorf("contree: base URL %q has an invalid hostname", value)
	}
	if port := parsed.Port(); port != "" {
		if _, err := strconv.ParseUint(port, 10, 16); err != nil {
			return "", fmt.Errorf(
				"contree: base URL %q has an invalid port: %w",
				value,
				err,
			)
		}
	}
	if parsed.User != nil {
		return "", errors.New("contree: base URL must not contain credentials")
	}
	if parsed.RawQuery != "" || parsed.Fragment != "" {
		return "", errors.New("contree: base URL must not contain a query or fragment")
	}
	return strings.TrimRight(parsed.String(), "/"), nil
}

func defaultUserAgent(identity string) string {
	parts := []string{
		identity,
		"contree-client-go/" + moduleVersion(),
		"Go/" + strings.TrimPrefix(runtime.Version(), "go"),
		runtime.GOOS + "/" + runtime.GOARCH,
	}
	result := parts[:0]
	for _, part := range parts {
		if part != "" {
			result = append(result, part)
		}
	}
	return strings.Join(result, " ")
}

func moduleVersion() string {
	info, ok := debug.ReadBuildInfo()
	if !ok {
		return "unknown"
	}
	if info.Main.Path == modulePath {
		return cleanModuleVersion(info.Main.Version)
	}
	for _, dependency := range info.Deps {
		if dependency.Path == modulePath {
			return cleanModuleVersion(dependency.Version)
		}
	}
	return "devel"
}

func cleanModuleVersion(version string) string {
	version = strings.TrimPrefix(version, "v")
	if version == "" || version == "(devel)" {
		return "devel"
	}
	return version
}

func (c *Client) do(ctx context.Context, spec requestSpec) (*http.Response, error) {
	return c.execute(ctx, spec, true, true)
}

func (c *Client) doStream(ctx context.Context, spec requestSpec) (*http.Response, error) {
	return c.execute(ctx, spec, false, false)
}

func (c *Client) doRawStream(
	ctx context.Context,
	spec requestSpec,
) (*http.Response, error) {
	spec.headers = spec.headers.Clone()
	if spec.headers == nil {
		spec.headers = make(http.Header)
	}
	if spec.headers.Get("Accept-Encoding") == "" {
		spec.headers.Set("Accept-Encoding", "gzip")
	}
	return c.execute(ctx, spec, false, false)
}

func (c *Client) execute(
	ctx context.Context,
	spec requestSpec,
	useDefaultTimeout bool,
	allowRetry bool,
) (*http.Response, error) {
	if ctx == nil {
		return nil, errors.New("contree: context must not be nil")
	}
	requestContext := ctx
	cancel := func() {}
	if useDefaultTimeout && c.timeout > 0 {
		requestContext, cancel = context.WithTimeout(ctx, c.timeout)
	}
	requestURL := c.baseURL + apiPathPrefix + spec.path
	if len(spec.query) > 0 {
		requestURL += "?" + encodeQuery(spec.query)
	}
	policy := c.retry
	if !allowRetry {
		policy = nil
	}
	attempts := 1
	if policy != nil {
		attempts = policy.MaxAttempts
	}
	for attempt := 1; attempt <= attempts; attempt++ {
		response, err := c.doAttempt(requestContext, requestURL, spec)
		if err != nil {
			var bodyError *requestBodyOpenError
			if errors.As(err, &bodyError) {
				cancel()
				return nil, bodyError.Err
			}
			if attempt == attempts || !canRetryTransport(spec, policy) {
				cancel()
				return nil, connectionError(spec.method, requestURL, err)
			}
		} else if attempt == attempts || !canRetryResponse(spec, response, policy) {
			response.Body = &cancelBody{ReadCloser: response.Body, cancel: cancel}
			return response, nil
		}

		var retryAfter time.Duration
		var hasRetryAfter bool
		if response != nil {
			retryAfter, hasRetryAfter = parseRetryAfter(
				response.Header.Get("Retry-After"),
				time.Now(),
			)
			drainAndClose(response.Body)
		}
		delay := retryDelay(policy, attempt, retryAfter, hasRetryAfter)
		if err := waitForRetry(requestContext, delay); err != nil {
			cancel()
			return nil, connectionError(spec.method, requestURL, err)
		}
	}
	cancel()
	return nil, errors.New("contree: exhausted request attempts")
}

func (c *Client) doAttempt(
	ctx context.Context,
	requestURL string,
	spec requestSpec,
) (*http.Response, error) {
	var body io.ReadCloser
	var err error
	if spec.body != nil {
		body, err = spec.body.open()
		if err != nil {
			return nil, &requestBodyOpenError{Err: err}
		}
	}
	request, err := http.NewRequestWithContext(ctx, spec.method, requestURL, body)
	if err != nil {
		if body != nil {
			_ = body.Close()
		}
		return nil, err
	}
	for name, values := range spec.headers {
		for _, value := range values {
			request.Header.Add(name, value)
		}
	}
	if c.token != "" && request.Header.Get("Authorization") == "" {
		request.Header.Set("Authorization", "Bearer "+c.token)
	}
	if c.project != "" && request.Header.Get("Project") == "" {
		request.Header.Set("Project", c.project)
	}
	if spec.contentType != "" && request.Header.Get("Content-Type") == "" {
		request.Header.Set("Content-Type", spec.contentType)
	}
	if spec.accept != "" && request.Header.Get("Accept") == "" {
		request.Header.Set("Accept", spec.accept)
	}
	if c.userAgent != "" && request.Header.Get("User-Agent") == "" {
		request.Header.Set("User-Agent", c.userAgent)
	}
	return c.http.Do(request)
}

func connectionError(method, requestURL string, err error) error {
	timedOut := errors.Is(err, context.DeadlineExceeded)
	var networkError net.Error
	if errors.As(err, &networkError) && networkError.Timeout() {
		timedOut = true
	}
	return &APIConnectionError{
		Method:   method,
		URL:      requestURL,
		TimedOut: timedOut,
		Err:      err,
	}
}

type requestBodyOpenError struct{ Err error }

func (e *requestBodyOpenError) Error() string { return e.Err.Error() }
func (e *requestBodyOpenError) Unwrap() error { return e.Err }

func canReplayBody(spec requestSpec) bool {
	return spec.body == nil || spec.body.replayable
}

func canRetryTransport(spec requestSpec, policy *RetryPolicy) bool {
	return policy != nil && canReplayBody(spec) && (spec.idempotent || policy.RetryUnsafe)
}

func canRetryResponse(
	spec requestSpec,
	response *http.Response,
	policy *RetryPolicy,
) bool {
	if policy == nil || !policy.retryableStatus(response.StatusCode) || !canReplayBody(spec) {
		return false
	}
	if response.StatusCode == 425 || response.StatusCode == http.StatusTooManyRequests {
		return true
	}
	return spec.idempotent || policy.RetryUnsafe
}

func (p RetryPolicy) retryableStatus(status int) bool {
	for _, configured := range p.Statuses {
		if status == configured {
			return true
		}
	}
	return p.ServerErrors && status >= 500 && status < 600
}

func retryDelay(
	policy *RetryPolicy,
	attempt int,
	serverDelay time.Duration,
	hasServerDelay bool,
) time.Duration {
	if hasServerDelay {
		return serverDelay
	}
	if policy == nil {
		return 0
	}
	index := attempt - 1
	if index >= len(policy.Delays) {
		index = len(policy.Delays) - 1
	}
	return policy.Delays[index]
}

func waitForRetry(ctx context.Context, delay time.Duration) error {
	if delay <= 0 {
		select {
		case <-ctx.Done():
			return ctx.Err()
		default:
			return nil
		}
	}
	timer := time.NewTimer(delay)
	defer timer.Stop()
	select {
	case <-ctx.Done():
		return ctx.Err()
	case <-timer.C:
		return nil
	}
}

func parseRetryAfter(value string, now time.Time) (time.Duration, bool) {
	value = strings.TrimSpace(value)
	if value == "" {
		return 0, false
	}
	seconds, err := strconv.ParseFloat(value, 64)
	if err == nil {
		if math.IsNaN(seconds) || math.IsInf(seconds, 0) {
			return 0, false
		}
		seconds = math.Max(0, seconds)
		maximum := float64(math.MaxInt64) / float64(time.Second)
		if seconds >= maximum {
			return time.Duration(math.MaxInt64), true
		}
		return time.Duration(seconds * float64(time.Second)), true
	}
	date, err := http.ParseTime(value)
	if err != nil {
		return 0, false
	}
	return max(0, date.Sub(now)), true
}

type cancelBody struct {
	io.ReadCloser
	cancel context.CancelFunc
}

func (b *cancelBody) Close() error {
	err := b.ReadCloser.Close()
	b.cancel()
	return err
}
