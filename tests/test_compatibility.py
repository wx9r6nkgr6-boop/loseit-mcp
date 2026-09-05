"""Credential-free GWT compatibility discovery."""

from __future__ import annotations

import json

import pytest

from loseit_mcp.compatibility import (
    CompatibilityError,
    decode_cache_bundle,
    discover_compatibility,
    extract_chromium_strong_name,
    extract_policy_hash,
    validate_policy,
)
from loseit_mcp.config import DEFAULT_POLICY_HASH, DEFAULT_STRONG_NAME


def _bootstrap(strong_name: str = DEFAULT_STRONG_NAME) -> str:
    return (
        "function web(){var A='default',B='safari',C='user.agent',"
        f"D='{strong_name}';function H(){{function k(a,b){{}}k([A,B],D);}}}}"
    )


def _bundle(strong_name: str = DEFAULT_STRONG_NAME, policy: str = DEFAULT_POLICY_HASH) -> str:
    javascript = (
        f"var $strongName = '{strong_name}';"
        "setHeader('X-GWT-Permutation',$strongName);"
        "var endpoint='web/service';"
        f"Proxy.call(this,module,null,'{policy}',serializer);"
    )
    return "web.onScriptDownloaded(" + json.dumps([javascript]) + ");"


def _policy() -> str:
    return (
        "com.loseit.core.client.service.LoseItRemoteService, false, false, false, false, _\n"
        "com.loseit.core.client.service.ServiceRequestToken/1076571655\n"
        "com.loseit.core.client.model.UserId/4281239478"
    )


def test_extracts_chromium_permutation_without_fixed_minifier_names() -> None:
    assert extract_chromium_strong_name(_bootstrap()) == DEFAULT_STRONG_NAME


def test_decodes_bundle_and_extracts_validated_policy() -> None:
    decoded = decode_cache_bundle(_bundle())
    assert extract_policy_hash(decoded, DEFAULT_STRONG_NAME) == DEFAULT_POLICY_HASH
    validate_policy(_policy())


def test_discovery_uses_only_the_three_expected_public_assets() -> None:
    root = "https://static.example/web/"
    assets = {
        root + "web.nocache.js": _bootstrap(),
        root + DEFAULT_STRONG_NAME + ".cache.js": _bundle(),
        root + DEFAULT_POLICY_HASH + ".gwt.rpc": _policy(),
    }
    requested: list[str] = []

    def fetch(url: str) -> str:
        requested.append(url)
        return assets[url]

    report = discover_compatibility(fetch, base_url=root)
    assert requested == list(assets)
    assert report.authentication_used is False
    assert report.configuration_changed is False
    assert report.strong_name_current is True
    assert report.policy_hash_current is True


def test_rejects_ambiguous_or_wrong_artifacts() -> None:
    with pytest.raises(CompatibilityError, match="one Chromium"):
        extract_chromium_strong_name("var A='default',B='safari';")
    with pytest.raises(CompatibilityError, match="selected strong name"):
        extract_policy_hash(decode_cache_bundle(_bundle("A" * 32)), "B" * 32)
    with pytest.raises(CompatibilityError, match="missing required"):
        validate_policy("not a Lose It policy")
