from __future__ import annotations

import json
from collections.abc import Callable
from types import ModuleType
from typing import Any

import pytest

from tests import stub_server as stub
from tests.stub_server import StubServer


def test_network_policy_methods(
    invoke: Callable[..., Any], models: ModuleType, stub_server: StubServer
) -> None:
    rule = models.NetworkPolicyCIDRRule.from_dict(stub.NETWORK_RULE)
    policy = invoke("create_network_policy", cidrs=[rule])
    assert policy.to_dict() == stub.NETWORK_POLICY
    assert json.loads(stub_server.last.body) == {"cidrs": [stub.NETWORK_RULE]}
    assert invoke("get_network_policy", policy.uuid).to_dict() == stub.NETWORK_POLICY

    search = "tcp://[2001:db8::/64]:443"
    listing = invoke("list_network_policies", limit=2, offset=1, search=search)
    assert listing.policies[0].uuid == policy.uuid
    assert listing.policies[0].summary.cidrs == 1
    assert listing.policies[0].created_at.year == 2024
    assert stub_server.last.query == {
        "limit": ["2"],
        "offset": ["1"],
        "search": [search],
    }

    tag = invoke("set_network_policy_tag", "tests/https", uuid=policy.uuid)
    assert tag.to_dict() == stub.NETWORK_TAG
    assert json.loads(stub_server.last.body) == {"uuid": policy.uuid}
    before = len(stub_server.captured)
    assert invoke("get_network_policy_tag", "tests/https").to_dict() == stub.NETWORK_TAG
    assert len(stub_server.captured) == before + 1
    tags = invoke("list_network_policy_tags", limit=2, offset=1)
    assert tags.tags[0].to_dict() == stub.NETWORK_TAG
    assert stub_server.last.query == {"limit": ["2"], "offset": ["1"]}
    assert invoke("delete_network_policy_tag", "tests/https") is None
    assert invoke("delete_network_policy", policy.uuid) is None


def test_networking_response_models(models: ModuleType) -> None:
    domain = {"domain": "*.example.com", "proto": "udp", "ports": [53]}
    payload = {
        "enabled": True,
        "policy": [stub.NETWORK_POLICY_UUID],
        "effective_policy": {"cidrs": [stub.NETWORK_RULE], "domains": [domain]},
    }
    for name in ("InstanceSpawnResponse", "OperationInstanceMetadata"):
        result = getattr(models, name).from_dict(
            {"command": "true", "image": stub.IMAGE_UUID, "networking": payload}
        )
        assert isinstance(result.networking, models.InstanceNetworkingResponse)
        assert result.networking.effective_policy.cidrs[0].ports == [443]
        assert isinstance(
            result.networking.effective_policy.domains[0],
            models.NetworkPolicyDomainRule,
        )
        assert result.to_dict()["networking"] == payload
    for extra in ({}, {"effective_policy": None}):
        result = models.InstanceNetworkingResponse.from_dict({"enabled": True, **extra})
        assert result.to_dict() == {"enabled": True, **extra}


@pytest.mark.parametrize("include_cidrs", [False, True])
@pytest.mark.parametrize("ports", [None, [], [443, 8443]])
def test_domain_policy_request(
    invoke: Callable[..., Any],
    models: ModuleType,
    stub_server: StubServer,
    include_cidrs: bool,
    ports: list[int] | None,
) -> None:
    domain: dict[str, Any] = {"domain": "**.example.com", "proto": "tcp"}
    if ports is not None:
        domain["ports"] = ports
    expected: dict[str, Any] = {"domains": [domain]}
    arguments: dict[str, Any] = {"domains": [models.NetworkPolicyDomainRule(**domain)]}
    if include_cidrs:
        expected["cidrs"] = [stub.NETWORK_RULE]
        arguments["cidrs"] = [models.NetworkPolicyCIDRRule(**stub.NETWORK_RULE)]

    invoke("create_network_policy", **arguments)

    assert json.loads(stub_server.last.body) == expected


def test_empty_policy_request(
    invoke: Callable[..., Any], stub_server: StubServer
) -> None:
    invoke("create_network_policy")
    assert json.loads(stub_server.last.body) == {}


def test_domain_policy_response(models: ModuleType) -> None:
    payload = {
        "uuid": stub.NETWORK_POLICY_UUID,
        "cidrs": [],
        "domains": [{"domain": "*.example.com", "proto": "udp", "ports": [53]}],
    }
    policy = models.NetworkPolicy.from_dict(payload)
    assert isinstance(policy.domains[0], models.NetworkPolicyDomainRule)
    assert policy.domains[0].domain == "*.example.com"
    assert policy.to_dict() == payload
