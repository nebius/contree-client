import assert from "node:assert/strict";
import { after, before, test } from "node:test";

import { ContreeClient } from "../lib/client.js";
import {
  InstanceNetworkingResponse,
  InstanceSpawnResponse,
  ListNetworkPolicyTagsResponse,
  NetworkPolicy,
  NetworkPolicyCIDRRule,
  NetworkPolicyTag,
  OperationInstanceMetadata,
} from "../lib/models.js";
import { startStub } from "./stub.mjs";

const policyUuid = "e166a653-c0ec-4c66-ae86-1463e22f5742";
const rule = { cidr: "192.0.2.0/24", proto: "tcp", ports: [443] };
const policy = { uuid: policyUuid, cidrs: [rule] };
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
  const created = await client.createNetworkPolicy([
    NetworkPolicyCIDRRule.fromWire(rule),
  ]);
  assert.ok(created instanceof NetworkPolicy);
  assert.deepEqual(created.toWire(), policy);
  assert.deepEqual(JSON.parse(requests.at(-1).options.body), { cidrs: [rule] });

  const fetched = await client.getNetworkPolicy(policyUuid);
  assert.deepEqual(fetched.toWire(), policy);
  assert.ok(fetched.cidrs[0] instanceof NetworkPolicyCIDRRule);

  const search = "tcp://[2001:db8::/64]:443";
  const listing = await client.listNetworkPolicies({
    limit: 2,
    offset: 1,
    search,
  });
  assert.equal(listing.policies[0].uuid, policyUuid);
  assert.equal(listing.policies[0].summary.cidrs, 1);
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

test("networking responses decode effective policy and preserve optional fields", () => {
  const networking = {
    enabled: true,
    policy: [policyUuid],
    effective_policy: { cidrs: [rule] },
  };
  for (const Model of [InstanceSpawnResponse, OperationInstanceMetadata]) {
    const result = Model.fromWire({ networking });
    assert.ok(result.networking instanceof InstanceNetworkingResponse);
    assert.ok(
      result.networking.effectivePolicy.cidrs[0] instanceof
        NetworkPolicyCIDRRule,
    );
    assert.deepEqual(result.toWire().networking, networking);
  }
  for (const extra of [{}, { effective_policy: null }]) {
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
