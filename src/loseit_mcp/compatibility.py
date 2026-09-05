"""Credential-free discovery of Lose It!'s deployed GWT build identifiers.

This module deliberately reads only public static assets.  It never loads a
``liauth`` token, calls ``/web/service``, or changes local configuration.  The
ordinary read/sync paths do not import it, so deployment checks remain an
explicit operator action.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import asdict, dataclass

import httpx

from .config import DEFAULT_BASE_URL, DEFAULT_POLICY_HASH, DEFAULT_STRONG_NAME

_HASH = r"[0-9A-F]{32}"
_ASSIGNMENT_RE = re.compile(r"(?P<name>[A-Za-z_$][\w$]*)='(?P<value>[^']*)'")
_POLICY_RE = re.compile(rf"null,'(?P<hash>{_HASH})'")
_MAX_ASSET_BYTES = 5_000_000


class CompatibilityError(RuntimeError):
    """A public GWT artifact was unavailable or did not match the expected shape."""


@dataclass(frozen=True)
class CompatibilityReport:
    """Identifiers discovered from one internally consistent public deployment."""

    strong_name: str
    policy_hash: str
    configured_strong_name: str
    configured_policy_hash: str
    strong_name_current: bool
    policy_hash_current: bool
    bootstrap_url: str
    bundle_url: str
    policy_url: str
    policy_validated: bool
    authentication_used: bool = False
    configuration_changed: bool = False

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _assignments(source: str) -> dict[str, str]:
    return {match.group("name"): match.group("value") for match in _ASSIGNMENT_RE.finditer(source)}


def extract_chromium_strong_name(bootstrap: str) -> str:
    """Extract the default-English WebKit/Chromium permutation.

    GWT labels WebKit-family browsers ``safari`` even when the user agent is
    Chrome/Chromium.  We resolve the minifier's variable names rather than
    relying on their current spellings (``Mb``, ``Yb``, ``tc``).
    """
    values = _assignments(bootstrap)
    default_vars = [name for name, value in values.items() if value == "default"]
    safari_vars = [name for name, value in values.items() if value == "safari"]
    if not default_vars or not safari_vars:
        raise CompatibilityError("Could not identify default/safari GWT properties")

    candidates: set[str] = set()
    for default_var in default_vars:
        for safari_var in safari_vars:
            mapping = re.compile(
                rf"\bk\(\[{re.escape(default_var)},{re.escape(safari_var)}\],"
                rf"(?P<bundle>[A-Za-z_$][\w$]*)\)"
            )
            for match in mapping.finditer(bootstrap):
                value = values.get(match.group("bundle"), "")
                if re.fullmatch(_HASH, value):
                    candidates.add(value)

    if len(candidates) != 1:
        raise CompatibilityError(
            f"Expected one Chromium GWT permutation, found {len(candidates)}"
        )
    return candidates.pop()


def decode_cache_bundle(wrapper: str) -> str:
    """Decode the JavaScript string carried by ``onScriptDownloaded([...])``."""
    try:
        start = wrapper.index("(") + 1
        end = wrapper.rindex(")")
        payload = json.loads(wrapper[start:end])
    except (ValueError, json.JSONDecodeError) as exc:
        raise CompatibilityError("Could not decode the GWT cache bundle") from exc
    if not isinstance(payload, list) or len(payload) != 1 or not isinstance(payload[0], str):
        raise CompatibilityError("Unexpected GWT cache bundle wrapper")
    return payload[0]


def extract_policy_hash(bundle: str, strong_name: str) -> str:
    """Extract the serialization policy embedded in the service proxy."""
    if f"$strongName = '{strong_name}'" not in bundle:
        raise CompatibilityError("Cache bundle does not declare the selected strong name")
    if "X-GWT-Permutation" not in bundle and "x-gwt-permutation" not in bundle.lower():
        raise CompatibilityError("Cache bundle does not set the GWT permutation header")
    if "web/service" not in bundle:
        raise CompatibilityError("Cache bundle does not contain the Lose It! service proxy")

    candidates = {match.group("hash") for match in _POLICY_RE.finditer(bundle)}
    if len(candidates) != 1:
        raise CompatibilityError(
            f"Expected one GWT serialization policy candidate, found {len(candidates)}"
        )
    return candidates.pop()


def validate_policy(policy: str) -> None:
    """Require the declarations used by the fork's read-only bootstrap call."""
    required = (
        "com.loseit.core.client.service.LoseItRemoteService",
        "com.loseit.core.client.service.ServiceRequestToken/1076571655",
        "com.loseit.core.client.model.UserId/4281239478",
    )
    missing = [declaration for declaration in required if declaration not in policy]
    if missing:
        raise CompatibilityError("GWT policy is missing required read-only service declarations")


def discover_compatibility(
    fetch_text: Callable[[str], str],
    *,
    base_url: str = DEFAULT_BASE_URL,
) -> CompatibilityReport:
    """Discover and cross-check current identifiers using public GETs only."""
    root = base_url.rstrip("/") + "/"
    bootstrap_url = root + "web.nocache.js"
    bootstrap = fetch_text(bootstrap_url)
    strong_name = extract_chromium_strong_name(bootstrap)

    bundle_url = root + strong_name + ".cache.js"
    bundle = decode_cache_bundle(fetch_text(bundle_url))
    policy_hash = extract_policy_hash(bundle, strong_name)

    policy_url = root + policy_hash + ".gwt.rpc"
    validate_policy(fetch_text(policy_url))
    return CompatibilityReport(
        strong_name=strong_name,
        policy_hash=policy_hash,
        configured_strong_name=DEFAULT_STRONG_NAME,
        configured_policy_hash=DEFAULT_POLICY_HASH,
        strong_name_current=strong_name == DEFAULT_STRONG_NAME,
        policy_hash_current=policy_hash == DEFAULT_POLICY_HASH,
        bootstrap_url=bootstrap_url,
        bundle_url=bundle_url,
        policy_url=policy_url,
        policy_validated=True,
    )


def fetch_current_compatibility(*, timeout: float = 30.0) -> CompatibilityReport:
    """Fetch the public deployment assets and return a validated drift report."""

    def fetch_text(url: str) -> str:
        try:
            response = client.get(url)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise CompatibilityError(f"Could not fetch public GWT asset {url}: {exc}") from exc
        if len(response.content) > _MAX_ASSET_BYTES:
            raise CompatibilityError(f"Public GWT asset is unexpectedly large: {url}")
        return response.text

    headers = {"user-agent": "Mozilla/5.0 (compatible; loseit-readonly-compatibility/1)"}
    with httpx.Client(headers=headers, timeout=timeout, follow_redirects=True) as client:
        return discover_compatibility(fetch_text)
