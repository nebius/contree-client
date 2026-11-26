import assert from "node:assert/strict";
import { after, before, test } from "node:test";

import { ContreeClient } from "../lib/client.js";
import {
  InstanceNetworkingResponse,
  InstanceSpawnResponse,
  ListNetworkPolicyTagsResponse,
  NetworkPolicy,
  NetworkPolicyCIDRRule,
  NetworkPolicyDomainRule,
  NetworkPolicyTag,
  OperationInstanceMetadata,
} from "../lib/models.js";
import { startStub } from "./stub.mjs";

const policyUuid = "e166a653-c0ec-4c66-ae86-1463e22f5742";
const rule = { cidr: "192.0.2.0/24", proto: "tcp", ports: [443] };
const domainRule = { domain: "**.example.com", proto: "udp", ports: [53] };
const policy = { uuid: policyUuid, cidrs: [rule], domains: [domainRule] };
const tag = { tag: "tests/https", uuid: policyUuid };
const requests = [];
let stub;
let client;

before(async () => {
  stub = await startStub();
  client = new ContreeClient("test-token", {
    baseUrl: stub.baseUrl,
    fetch: (url, options) => {
      requests.push({ url: new URL(url), options });
      return fetch(url, options);
    },
  });
});

after(async () => {
  await client.close();
  stub.stop();
});

test("network policy methods preserve typed responses and tag redirect bodies", async () => {
  const created = await client.createNetworkPolicy({
    cidrs: [NetworkPolicyCIDRRule.fromWire(rule)],
    domains: [NetworkPolicyDomainRule.fromWire(domainRule)],
  });
  assert.ok(created instanceof NetworkPolicy);
  assert.deepEqual(created.toWire(), policy);
  assert.ok(created.domains[0] instanceof NetworkPolicyDomainRule);
  assert.deepEqual(JSON.parse(requests.at(-1).options.body), {
    cidrs: [rule],
    domains: [domainRule],
  });

  const fetched = await client.getNetworkPolicy(policyUuid);
  assert.deepEqual(fetched.toWire(), policy);
  assert.ok(fetched.cidrs[0] instanceof NetworkPolicyCIDRRule);
  assert.ok(fetched.domains[0] instanceof NetworkPolicyDomainRule);

  const search = "tcp://[2001:db8::/64]:443";
  const listing = await client.listNetworkPolicies({
    limit: 2,
    offset: 1,
    search,
  });
  assert.equal(listing.policies[0].uuid, policyUuid);
  assert.equal(listing.policies[0].summary.cidrs, 1);
  assert.equal(listing.policies[0].summary.domains, 1);
  assert.equal(listing.policies[0].createdAt.getUTCFullYear(), 2024);
  assert.equal(requests.at(-1).url.searchParams.get("limit"), "2");
  assert.equal(requests.at(-1).url.searchParams.get("offset"), "1");
  assert.equal(requests.at(-1).url.searchParams.get("search"), search);

  const assigned = await client.setNetworkPolicyTag("tests/https", policyUuid);
  assert.ok(assigned instanceof NetworkPolicyTag);
  assert.deepEqual(assigned.toWire(), tag);
  assert.deepEqual(JSON.parse(requests.at(-1).options.body), {
    uuid: policyUuid,
  });

  const resolved = await client.getNetworkPolicyTag("tests/https");
  assert.ok(resolved instanceof NetworkPolicyTag);
  assert.deepEqual(resolved.toWire(), tag);
  assert.equal(requests.at(-1).options.redirect, "manual");
  assert.ok(
    decodeURIComponent(requests.at(-1).url.pathname).endsWith(
      "/network/tags/tests/https",
    ),
  );

  const tags = await client.listNetworkPolicyTags({ limit: 2, offset: 1 });
  assert.ok(tags instanceof ListNetworkPolicyTagsResponse);
  assert.ok(tags.tags[0] instanceof NetworkPolicyTag);
  assert.deepEqual(tags.toWire(), { tags: [tag] });
  assert.equal(requests.at(-1).url.searchParams.get("limit"), "2");
  assert.equal(requests.at(-1).url.searchParams.get("offset"), "1");

  assert.equal(await client.deleteNetworkPolicyTag("tests/https"), null);
  assert.equal(await client.deleteNetworkPolicy(policyUuid), null);
});

test("CIDR-only policy requests omit domains", async () => {
  const created = await client.createNetworkPolicy({
    cidrs: [NetworkPolicyCIDRRule.fromWire(rule)],
  });

  assert.deepEqual(JSON.parse(requests.at(-1).options.body), { cidrs: [rule] });
  assert.deepEqual(created.toWire(), {
    uuid: policyUuid,
    cidrs: [rule],
    domains: [],
  });
});

for (const [name, options] of [
  ["legacy rule array", [NetworkPolicyCIDRRule.fromWire(rule)]],
  ["empty array", []],
  ["null", null],
  ["string", "cidrs"],
  ["number", 1],
  ["boolean", false],
  ["bigint", 1n],
  ["symbol", Symbol("options")],
  ["function", () => ({ cidrs: [rule] })],
]) {
  test(`network policy rejects ${name} before sending a request`, async () => {
    const requestCount = requests.length;

    await assert.rejects(client.createNetworkPolicy(options), {
      name: "TypeError",
      message: "options must be a non-null object, not an array",
    });

    assert.equal(requests.length, requestCount);
  });
}

test("class instances with supported properties are valid policy options", async () => {
  class PolicyOptions {
    cidrs = [NetworkPolicyCIDRRule.fromWire(rule)];
  }

  const created = await client.createNetworkPolicy(new PolicyOptions());

  assert.deepEqual(JSON.parse(requests.at(-1).options.body), { cidrs: [rule] });
  assert.deepEqual(created.toWire(), {
    uuid: policyUuid,
    cidrs: [rule],
    domains: [],
  });
});

for (const [key, options] of [
  ["cidr", { cidr: [rule] }],
  ["domain", { cidrs: [rule], domain: "example.com" }],
  ["toString", { toString: "unexpected" }],
  ["Symbol(cidrs)", { [Symbol("cidrs")]: [rule] }],
]) {
  test(`network policy rejects unknown option ${key} before sending a request`, async () => {
    const requestCount = requests.length;

    await assert.rejects(client.createNetworkPolicy(options), {
      name: "TypeError",
      message: `unknown option: ${key}`,
    });

    assert.equal(requests.length, requestCount);
  });
}

for (const ports of [undefined, [], [443, 8443]]) {
  for (const includeCidrs of [false, true]) {
    test(`domain policy request preserves ports=${JSON.stringify(ports)} and cidrs=${includeCidrs}`, async () => {
      const domain = { domain: "**.example.com", proto: "tcp" };
      if (ports !== undefined) domain.ports = ports;
      const options = { domains: [new NetworkPolicyDomainRule(domain)] };
      const expected = { domains: [domain] };
      if (includeCidrs) {
        options.cidrs = [NetworkPolicyCIDRRule.fromWire(rule)];
        expected.cidrs = [rule];
      }

      await client.createNetworkPolicy(options);

      assert.deepEqual(JSON.parse(requests.at(-1).options.body), expected);
    });
  }
}

test("empty policy request omits both rule groups", async () => {
  await client.createNetworkPolicy();
  assert.deepEqual(JSON.parse(requests.at(-1).options.body), {});
});

for (const [name, options] of [
  ["empty object", {}],
  ["undefined", undefined],
  ["null-prototype object", Object.create(null)],
]) {
  test(`${name} options create an empty policy`, async () => {
    await client.createNetworkPolicy(options);
    assert.deepEqual(JSON.parse(requests.at(-1).options.body), {});
  });
}

test("domain policy responses preserve typed rules", () => {
  const payload = {
    uuid: policyUuid,
    cidrs: [],
    domains: [{ domain: "*.example.com", proto: "udp", ports: [53] }],
  };
  const decoded = NetworkPolicy.fromWire(payload);
  assert.ok(decoded.domains[0] instanceof NetworkPolicyDomainRule);
  assert.equal(decoded.domains[0].domain, "*.example.com");
  assert.deepEqual(decoded.toWire(), payload);
});

test("networking responses decode effective policy and preserve optional fields", () => {
  const domain = { domain: "**.example.com", proto: "tcp", ports: [8443] };
  const networking = {
    enabled: true,
    policy: [policyUuid],
    effective_policy: { cidrs: [rule], domains: [domain] },
  };
  for (const Model of [InstanceSpawnResponse, OperationInstanceMetadata]) {
    const result = Model.fromWire({ networking });
    assert.ok(result.networking instanceof InstanceNetworkingResponse);
    assert.ok(
      result.networking.effectivePolicy.cidrs[0] instanceof
        NetworkPolicyCIDRRule,
    );
    assert.ok(
      result.networking.effectivePolicy.domains[0] instanceof
        NetworkPolicyDomainRule,
    );
    assert.deepEqual(
      result.networking.effectivePolicy.domains[0].toWire(),
      domain,
    );
    assert.deepEqual(result.toWire().networking, networking);
  }
  for (const extra of [
    {},
    { effective_policy: null },
    { effective_policy: { cidrs: [rule] } },
    { effective_policy: { domains: [domain] } },
  ]) {
    const wire = { enabled: true, ...extra };
    assert.deepEqual(InstanceNetworkingResponse.fromWire(wire).toWire(), wire);
  }
});

test("network tag lookup rejects a followed 200 response", async () => {
  const followed = new ContreeClient("test-token", {
    fetch: async () => new Response(JSON.stringify(policy), { status: 200 }),
  });
  try {
    await assert.rejects(followed.getNetworkPolicyTag("tests/https"), {
      name: "RangeError",
      message: "unexpected HTTP status 200",
    });
  } finally {
    await followed.close();
  }
});
