"""Owner-only local storage for optional research-provider credentials."""

from __future__ import annotations

import os
import stat
from pathlib import Path

from .auth import _write_private
from .config import CONFIG_DIR

USDA_API_KEY_FILE = CONFIG_DIR / "usda_fdc_api_key"


class CredentialError(RuntimeError):
    """A provider credential is absent, unsafe, or malformed."""


def save_usda_api_key(value: str, path: Path = USDA_API_KEY_FILE) -> None:
    """Save a FoodData Central key without ever creating a permissive file."""
    key = value.strip()
    if not key or len(key) > 512 or any(ch.isspace() for ch in key):
        raise CredentialError("Enter a valid FoodData Central API key.")
    _write_private(path, key + "\n")


def load_usda_api_key(path: Path = USDA_API_KEY_FILE) -> str | None:
    """Load the key, rejecting a provider-secret file readable by other users."""
    if not path.is_file():
        return None
    if os.name == "posix" and stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise CredentialError("The FoodData Central key file is not owner-only.")
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise CredentialError("The FoodData Central key could not be read.") from exc
    return value or None


def usda_configuration(path: Path = USDA_API_KEY_FILE) -> dict[str, str | bool]:
    """Credential-free provider status suitable for the dashboard."""
    try:
        configured = bool(load_usda_api_key(path))
    except CredentialError:
        return {"provider": "USDA FoodData Central", "configured": False, "status": "unsafe"}
    return {
        "provider": "USDA FoodData Central",
        "configured": configured,
        "status": "configured" if configured else "not_configured",
    }
