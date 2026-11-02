from __future__ import annotations

import json
from collections.abc import Callable
from types import ModuleType
from typing import Any

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
    payload = {
        "enabled": True,
        "policy": [stub.NETWORK_POLICY_UUID],
        "effective_policy": {"cidrs": [stub.NETWORK_RULE]},
    }
    for name in ("InstanceSpawnResponse", "OperationInstanceMetadata"):
        result = getattr(models, name).from_dict(
            {"command": "true", "image": stub.IMAGE_UUID, "networking": payload}
        )
        assert isinstance(result.networking, models.InstanceNetworkingResponse)
        assert result.networking.effective_policy.cidrs[0].ports == [443]
        assert result.to_dict()["networking"] == payload
    for extra in ({}, {"effective_policy": None}):
        result = models.InstanceNetworkingResponse.from_dict({"enabled": True, **extra})
        assert result.to_dict() == {"enabled": True, **extra}
