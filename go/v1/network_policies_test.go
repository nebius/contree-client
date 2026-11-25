package contree

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"reflect"
	"testing"
)

func TestNetworkPolicyOperations(t *testing.T) {
	const policyID = "01900000-0000-7000-8000-000000000001"
	const tag = "team/policy with space"
	const tagPath = "/v1/network/tags/team%2Fpolicy%20with%20space"
	const policyJSON = `{"uuid":"` + policyID + `","cidrs":[{"cidr":"192.0.2.0/24","proto":"tcp","ports":[443]}],"domains":[{"domain":"**.example.com","proto":"udp","ports":[53]}]}`
	const tagJSON = `{"tag":"` + tag + `","uuid":"` + policyID + `"}`
	type call struct {
		method, path, search, limit, offset string
		body                                []byte
	}
	calls := make(chan call, 16)
	server := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		body, err := io.ReadAll(request.Body)
		if err != nil {
			t.Error(err)
		}
		query := request.URL.Query()
		calls <- call{request.Method, request.URL.EscapedPath(), query.Get("search"), query.Get("limit"), query.Get("offset"), body}
		writer.Header().Set("Content-Type", "application/json")
		switch request.Method + " " + request.URL.EscapedPath() {
		case "POST /v1/network/policies":
			writer.WriteHeader(http.StatusCreated)
			io.WriteString(writer, policyJSON)
		case "GET /v1/network/policies":
			io.WriteString(writer, `{"policies":[{"uuid":"`+policyID+`","created_at":"2026-11-02T00:00:00Z","tags":["`+tag+`"],"summary":{"cidrs":1,"domains":1}}]}`)
		case "GET /v1/network/policies/" + policyID:
			io.WriteString(writer, policyJSON)
		case "GET /v1/network/tags":
			io.WriteString(writer, `{"tags":[`+tagJSON+`]}`)
		case "PUT " + tagPath:
			io.WriteString(writer, tagJSON)
		case "GET " + tagPath:
			writer.Header().Set("Location", "/v1/network/policies/"+policyID)
			writer.WriteHeader(http.StatusFound)
			io.WriteString(writer, tagJSON)
		case "DELETE " + tagPath, "DELETE /v1/network/policies/" + policyID:
			writer.WriteHeader(http.StatusNoContent)
		default:
			t.Errorf("unexpected request: %s %s", request.Method, request.URL)
			writer.WriteHeader(http.StatusNotFound)
		}
	}))
	defer server.Close()
	client, err := NewClient("test", WithBaseURL(server.URL))
	if err != nil {
		t.Fatal(err)
	}
	ctx := context.Background()
	checkCall := func(method, path string) call {
		t.Helper()
		select {
		case got := <-calls:
			if got.method != method || got.path != path {
				t.Fatalf("request = %s %s, want %s %s", got.method, got.path, method, path)
			}
			return got
		default:
			t.Fatal("expected an HTTP request")
			return call{}
		}
	}
	domainRules := []NetworkPolicyDomainRule{
		{Domain: "**.example.com", Proto: "udp", Ports: Some([]int64{53})},
	}
	created, err := client.CreateNetworkPolicy(ctx, &CreateNetworkPolicyOptions{
		Cidrs: Some([]NetworkPolicyCidrRule{
			{Cidr: "192.0.2.0/24", Proto: "tcp", Ports: Some([]int64{443})},
		}),
		Domains: Some(domainRules),
	})
	if err != nil || created.UUID != policyID || len(created.Cidrs) != 1 {
		t.Fatalf("CreateNetworkPolicy = %#v, %v", created, err)
	}
	if !reflect.DeepEqual(created.Domains, domainRules) {
		t.Fatalf("created domains = %#v, want %#v", created.Domains, domainRules)
	}
	request := checkCall("POST", "/v1/network/policies")
	var createPayload NetworkPolicyCreateRequest
	if err := json.Unmarshal(request.body, &createPayload); err != nil {
		t.Fatal(err)
	}
	cidrs, present := createPayload.Cidrs.Value()
	if !present || len(cidrs) != 1 {
		t.Fatalf("create payload = %s", request.body)
	}
	ports, present := cidrs[0].Ports.Value()
	if cidrs[0].Cidr != "192.0.2.0/24" || !present || !reflect.DeepEqual(ports, []int64{443}) {
		t.Fatalf("create payload = %s", request.body)
	}
	domains, present := createPayload.Domains.Value()
	if !present || !reflect.DeepEqual(domains, domainRules) {
		t.Fatalf("create payload domains = %#v, want %#v", domains, domainRules)
	}

	search, limit, offset := "udp://[2001:db8::/64]:53", int64(7), int64(0)
	listed, err := client.ListNetworkPolicies(ctx, &ListNetworkPoliciesOptions{Search: &search, Limit: &limit, Offset: &offset})
	if err != nil || len(listed.Policies) != 1 || listed.Policies[0].Summary.Cidrs != 1 || listed.Policies[0].CreatedAt.Year() != 2026 {
		t.Fatalf("ListNetworkPolicies = %#v, %v", listed, err)
	}
	if listed.Policies[0].Summary.Domains != 1 {
		t.Fatalf("domain summary = %d, want 1", listed.Policies[0].Summary.Domains)
	}
	request = checkCall("GET", "/v1/network/policies")
	if request.search != search || request.limit != "7" || request.offset != "0" {
		t.Fatalf("list query = %#v", request)
	}
	fetched, err := client.GetNetworkPolicy(ctx, policyID)
	if err != nil || !reflect.DeepEqual(fetched, created) {
		t.Fatalf("GetNetworkPolicy = %#v, %v", fetched, err)
	}
	checkCall("GET", "/v1/network/policies/"+policyID)
	bound, err := client.SetNetworkPolicyTag(ctx, tag, policyID)
	if err != nil || bound.Tag != tag || bound.UUID != policyID {
		t.Fatalf("SetNetworkPolicyTag = %#v, %v", bound, err)
	}
	request = checkCall("PUT", tagPath)
	var binding map[string]string
	if err := json.Unmarshal(request.body, &binding); err != nil || binding["uuid"] != policyID {
		t.Fatalf("tag binding body = %s, %v", request.body, err)
	}
	tags, err := client.ListNetworkPolicyTags(ctx, &ListNetworkPolicyTagsOptions{Limit: &limit, Offset: &offset})
	if err != nil || len(tags.Tags) != 1 || tags.Tags[0] != bound {
		t.Fatalf("ListNetworkPolicyTags = %#v, %v", tags, err)
	}
	request = checkCall("GET", "/v1/network/tags")
	if request.limit != "7" || request.offset != "0" {
		t.Fatalf("tag list query = %#v", request)
	}
	resolved, err := client.GetNetworkPolicyTag(ctx, tag)
	if err != nil || resolved != bound {
		t.Fatalf("GetNetworkPolicyTag must preserve the 302 JSON binding: %#v, %v", resolved, err)
	}
	checkCall("GET", tagPath)
	if len(calls) != 0 {
		t.Fatal("tag redirect was followed")
	}
	if err := client.DeleteNetworkPolicyTag(ctx, tag); err != nil {
		t.Fatal(err)
	}
	checkCall("DELETE", tagPath)
	if err := client.DeleteNetworkPolicy(ctx, policyID); err != nil {
		t.Fatal(err)
	}
	checkCall("DELETE", "/v1/network/policies/"+policyID)
	if len(calls) != 0 {
		t.Fatal("unexpected extra HTTP requests")
	}
}

func TestDomainPolicyRequests(t *testing.T) {
	for _, test := range []struct {
		name    string
		options *CreateNetworkPolicyOptions
		body    string
	}{
		{"empty", nil, `{}`},
		{
			"CIDR only",
			&CreateNetworkPolicyOptions{Cidrs: Some([]NetworkPolicyCidrRule{
				{Cidr: "192.0.2.0/24", Proto: "tcp", Ports: Some([]int64{443})},
			})},
			`{"cidrs":[{"cidr":"192.0.2.0/24","proto":"tcp","ports":[443]}]}`,
		},
		{
			"domain only",
			&CreateNetworkPolicyOptions{Domains: Some([]NetworkPolicyDomainRule{
				{Domain: "**.example.com", Proto: "tcp", Ports: Some([]int64{443, 8443})},
			})},
			`{"domains":[{"domain":"**.example.com","proto":"tcp","ports":[443,8443]}]}`,
		},
		{
			"all ports omitted",
			&CreateNetworkPolicyOptions{Domains: Some([]NetworkPolicyDomainRule{
				{Domain: "*.example.com", Proto: "udp"},
			})},
			`{"domains":[{"domain":"*.example.com","proto":"udp"}]}`,
		},
		{
			"mixed with explicit all ports",
			&CreateNetworkPolicyOptions{
				Cidrs: Some([]NetworkPolicyCidrRule{{Cidr: "192.0.2.0/24", Proto: "tcp"}}),
				Domains: Some([]NetworkPolicyDomainRule{
					{Domain: "example.com", Proto: "udp", Ports: Some([]int64{})},
				}),
			},
			`{"cidrs":[{"cidr":"192.0.2.0/24","proto":"tcp"}],"domains":[{"domain":"example.com","proto":"udp","ports":[]}]}`,
		},
	} {
		t.Run(test.name, func(t *testing.T) {
			requests := make(chan []byte, 1)
			server := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
				if request.Method != http.MethodPost || request.URL.Path != "/v1/network/policies" {
					t.Errorf("unexpected request: %s %s", request.Method, request.URL.Path)
				}
				requestBody, err := io.ReadAll(request.Body)
				if err != nil {
					t.Error(err)
				}
				requests <- requestBody
				writer.Header().Set("Content-Type", "application/json")
				writer.WriteHeader(http.StatusCreated)
				io.WriteString(writer, `{"uuid":"policy","cidrs":[],"domains":[{"domain":"*.example.com","proto":"udp","ports":[53]}]}`)
			}))
			defer server.Close()
			client, err := NewClient("test", WithBaseURL(server.URL))
			if err != nil {
				t.Fatal(err)
			}

			policy, err := client.CreateNetworkPolicy(context.Background(), test.options)
			if err != nil {
				t.Fatal(err)
			}
			requestBody := <-requests
			var got, want any
			if err := json.Unmarshal(requestBody, &got); err != nil {
				t.Fatal(err)
			}
			if err := json.Unmarshal([]byte(test.body), &want); err != nil {
				t.Fatal(err)
			}
			if !reflect.DeepEqual(got, want) {
				t.Fatalf("request body = %s, want %s", requestBody, test.body)
			}
			if len(policy.Domains) != 1 || policy.Domains[0].Domain != "*.example.com" || policy.Domains[0].Proto != "udp" {
				t.Fatalf("domain response = %#v", policy.Domains)
			}
			ports, present := policy.Domains[0].Ports.Value()
			if !present || !reflect.DeepEqual(ports, []int64{53}) {
				t.Fatalf("domain response ports = %#v", policy.Domains[0].Ports)
			}
		})
	}
}

func TestInstanceNetworkingResponseEffectivePolicy(t *testing.T) {
	for _, test := range []struct {
		name, field string
		set, null   bool
		domains     []NetworkPolicyDomainRule
	}{
		{"omitted", "", false, false, nil},
		{"null", `,"effective_policy":null`, true, true, nil},
		{"empty", `,"effective_policy":{"cidrs":[]}`, true, false, nil},
		{
			"domain",
			`,"effective_policy":{"cidrs":[],"domains":[{"domain":"**.example.com","proto":"tcp","ports":[8443]}]}`,
			true, false,
			[]NetworkPolicyDomainRule{{Domain: "**.example.com", Proto: "tcp", Ports: Some([]int64{8443})}},
		},
	} {
		t.Run(test.name, func(t *testing.T) {
			payload := []byte(`{"networking":{"enabled":true,"policy":[]` + test.field + `}}`)
			var response InstanceSpawnResponse
			if err := json.Unmarshal(payload, &response); err != nil {
				t.Fatal(err)
			}
			networking, present := response.Networking.Value()
			if !present || networking.EffectivePolicy.IsSet() != test.set || networking.EffectivePolicy.IsNull() != test.null {
				t.Fatalf("networking field state = %#v", networking)
			}
			if policy, present := networking.EffectivePolicy.Value(); present {
				cidrs, present := policy.Cidrs.Value()
				if !present || cidrs == nil || len(cidrs) != 0 {
					t.Fatalf("effective CIDRs = %#v, want an empty list", policy.Cidrs)
				}
				domains, present := policy.Domains.Value()
				if present != (test.domains != nil) || !reflect.DeepEqual(domains, test.domains) {
					t.Fatalf("effective domains = %#v, want %#v", policy.Domains, test.domains)
				}
			}
			encoded, err := json.Marshal(response)
			if err != nil {
				t.Fatal(err)
			}
			var got, want any
			if err := json.Unmarshal(encoded, &got); err != nil {
				t.Fatal(err)
			}
			if err := json.Unmarshal(payload, &want); err != nil {
				t.Fatal(err)
			}
			if !reflect.DeepEqual(got, want) {
				t.Fatalf("networking serialization changed: %s -> %s", payload, encoded)
			}
		})
	}
}
