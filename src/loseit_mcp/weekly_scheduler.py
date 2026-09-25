"""Separate, low-interference weekly food-pattern audit LaunchAgent."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import plistlib
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

from .food_patterns import weekly_audit

LABEL = "com.local.loseit-readonly.weekly-pattern-audit"
SCHEDULE = {"Weekday": 7, "Hour": 11, "Minute": 30}  # Sunday local time


def launch_agent_path(home: Path | None = None) -> Path:
    return (home or Path.home()) / "Library/LaunchAgents" / f"{LABEL}.plist"


def generate_configuration(data_dir: Path, *, python: str | None = None) -> dict:
    data = data_dir.expanduser().resolve()
    return {
        "Label": LABEL,
        "ProgramArguments": [python or sys.executable, "-m", "loseit_mcp.weekly_scheduler",
                             "run", "--data-dir", str(data)],
        "RunAtLoad": False,
        "StartCalendarInterval": SCHEDULE,
        "ProcessType": "Background", "LowPriorityIO": True, "Umask": 0o077,
        "StandardOutPath": str(data / "logs/weekly-pattern-audit.log"),
        "StandardErrorPath": str(data / "logs/weekly-pattern-audit-error.log"),
        "EnvironmentVariables": {"PYTHONUNBUFFERED": "1"},
    }


def install(data_dir: Path, *, home: Path | None = None, load: bool = True) -> dict:
    data = data_dir.expanduser().resolve()
    if not (data / "nutrition.sqlite3").is_file():
        raise ValueError("Existing nutrition repository required")
    (data / "logs").mkdir(parents=True, exist_ok=True, mode=0o700)
    target = launch_agent_path(home)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = plistlib.dumps(generate_configuration(data), fmt=plistlib.FMT_XML, sort_keys=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{LABEL}.", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, target)
    finally:
        Path(temporary).unlink(missing_ok=True)
    if load:
        domain = f"gui/{os.getuid()}"
        subprocess.run(["launchctl", "bootout", domain, str(target)],
                       capture_output=True, check=False, timeout=20)
        subprocess.run(["launchctl", "enable", f"{domain}/{LABEL}"],
                       capture_output=True, check=False, timeout=20)
        subprocess.run(["launchctl", "bootstrap", domain, str(target)],
                       capture_output=True, check=True, timeout=20)
    return {"installed": True, "path": str(target), "schedule": SCHEDULE}


def run(data_dir: Path, *, now: datetime | None = None) -> dict:
    current = now or datetime.now().astimezone()
    data = data_dir.expanduser().resolve()
    if not (data / "nutrition.sqlite3").is_file():
        raise ValueError("Existing nutrition repository required")
    with (data / "backfill.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"status": "busy", "completed_at": current.isoformat()}
        # Scheduled runs are local-only. Sending private food names/brands to
        # an external provider requires a separately reviewed authorization.
        result = weekly_audit(data, today=current.date())
        publication = None
        if result["classifications_created"] or result["research_classifications_created"]:
            from .publication import publish_snapshot

            publication = publish_snapshot(data, today=current.date())
        return {"status": "complete" if result["provider_failures"] == 0 else "provider_failed",
                "completed_at": current.isoformat(), "static_snapshot": publication, **result}


def main(argv=None) -> int:
    from .sync_cli import default_data_dir

    parser = argparse.ArgumentParser(description="Weekly local food-pattern audit")
    parser.add_argument("action", choices=("run", "install"))
    parser.add_argument("--data-dir", type=Path, default=default_data_dir())
    args = parser.parse_args(argv)
    result = run(args.data_dir) if args.action == "run" else install(args.data_dir)
    print(json.dumps(result, allow_nan=False))
    return 0 if result.get("status") != "provider_failed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
