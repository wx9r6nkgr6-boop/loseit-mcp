"""Explicit, secret-safe Lose It reconnection helpers for the local dashboard."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from lose_it.core.auth import list_browser_profiles, refresh_token_from_browser

from .auth import AuthError, decode_jwt_payload, is_expired, load_token, save_token
from .config import ConfigError, Settings, load_settings
from .readonly_service import ReadOnlyLoseItService
from .service import _is_auth_failure

SUPPORTED_BROWSERS = ("chrome", "brave")


def connection_status(data_dir: Path | None = None, *, settings: Settings | None = None) -> dict:
    """Return a local-only status. It never validates remotely or reads browser cookies."""
    settings = settings or load_settings()
    try:
        token = settings.token or load_token(settings.token_file)
        state = "reconnect_required" if not token or is_expired(token) else "connected"
        claims = decode_jwt_payload(token) if token else None
        exp = claims.get("exp") if isinstance(claims, dict) else None
    except (AuthError, OSError):
        state, exp = "reconnect_required", None
    result = {
        "status": state,
        "expires_at": datetime.fromtimestamp(exp, UTC).isoformat()
        if isinstance(exp, int | float)
        else None,
        "last_auth_check": None,
        "last_successful_update": None,
        "latest_diary_date": None,
    }
    if data_dir is None:
        return result
    db = Path(data_dir).expanduser().resolve() / "nutrition.sqlite3"
    if not db.is_file():
        return result
    try:
        with closing(sqlite3.connect(f"file:{db}?mode=ro", uri=True)) as connection:
            connection.row_factory = sqlite3.Row
            tables = {
                row[0]
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            if "update_events" in tables:
                row = connection.execute(
                    "SELECT created_at FROM update_events WHERE stage='auth_checked' "
                    "ORDER BY id DESC LIMIT 1"
                ).fetchone()
                result["last_auth_check"] = row[0] if row else None
                row = connection.execute(
                    "SELECT created_at FROM update_events WHERE stage='finished' "
                    "AND json_extract(summary_json,'$.status') LIKE 'complete%' "
                    "ORDER BY id DESC LIMIT 1"
                ).fetchone()
                result["last_successful_update"] = row[0] if row else None
            if "food_occurrences" in tables:
                row = connection.execute(
                    "SELECT MAX(source_date) FROM food_occurrences WHERE is_current=1"
                ).fetchone()
                result["latest_diary_date"] = row[0] if row else None
    except sqlite3.Error:
        pass
    return result


def browser_profiles(browser: str) -> list[dict[str, str | None]]:
    """List safe display metadata only; cookie-store paths never reach the UI."""
    if browser not in SUPPORTED_BROWSERS:
        return []
    return [
        {"directory": row["directory"], "name": row.get("name")}
        for row in list_browser_profiles(browser)
    ]


def reconnect_with_token(
    token: str,
    *,
    settings: Settings | None = None,
    service_factory: Callable[[Settings], object] = ReadOnlyLoseItService,
    saver: Callable[[str, Path], None] = save_token,
) -> dict[str, str]:
    """Validate with one read-only RPC, then atomically replace the stored credential."""
    candidate = token.strip()
    if not candidate or decode_jwt_payload(candidate) is None or is_expired(candidate):
        return {
            "status": "failed",
            "message": "That Lose It session is missing, malformed, or expired.",
        }
    resolved = settings or load_settings()
    temporary = replace(resolved, token=candidate, persist_session=False)
    try:
        with service_factory(temporary) as service:
            service.search_food("water", limit=1, detail=False)
        saver(candidate, resolved.token_file)
    except Exception:  # noqa: BLE001 - secret-bearing boundary; never echo the exception
        return {
            "status": "failed",
            "message": "Lose It did not accept that session. Sign in again and retry.",
        }
    return {"status": "connected", "message": "Lose It reconnected securely."}


def reconnect_from_browser(
    browser: str,
    profile: str | None,
    *,
    loader: Callable[..., str | None] = refresh_token_from_browser,
    **kwargs,
) -> dict[str, str]:
    """Import one browser profile's liauth cookie, then use the same validation path."""
    if browser not in SUPPORTED_BROWSERS:
        return {"status": "failed", "message": "Choose Chrome or Brave."}
    try:
        token = loader(browser, profile=profile)
    except Exception:  # noqa: BLE001 - browser/keychain errors can contain local paths
        token = None
    if not token:
        return {
            "status": "failed",
            "message": "No current Lose It session was found in that browser profile.",
        }
    return reconnect_with_token(token, **kwargs)


def is_reconnect_error(exc: Exception) -> bool:
    """Classify only authentication failures; protocol/network failures stay distinct."""
    if isinstance(exc, ConfigError):
        text = str(exc).casefold()
        return "fresh login" in text or "session token" in text or "credentials" in text
    return _is_auth_failure(exc)
