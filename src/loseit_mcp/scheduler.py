"""Per-user macOS LaunchAgent for the authoritative update pipeline."""

from __future__ import annotations

import argparse
import json
import os
import plistlib
import subprocess
import sys
import tempfile
from datetime import datetime, time, timedelta
from pathlib import Path
from uuid import uuid4

from .proposals import reader
from .repository import NutritionRepository, _now

LABEL = "com.local.loseit-readonly.daily-update"
SCHEDULE_HOUR = 10


def launch_agent_path(home: Path | None = None) -> Path:
    return (home or Path.home()) / "Library/LaunchAgents" / f"{LABEL}.plist"


def generate_configuration(data_dir: Path, *, python: str | None = None) -> dict:
    data = data_dir.expanduser().resolve()
    log_dir = data / "logs"
    return {
        "Label": LABEL,
        "ProgramArguments": [
            python or sys.executable,
            "-m",
            "loseit_mcp.scheduler",
            "run",
            "--data-dir",
            str(data),
        ],
        "RunAtLoad": True,
        "StartCalendarInterval": {"Hour": SCHEDULE_HOUR, "Minute": 0},
        "ProcessType": "Background",
        "LowPriorityIO": True,
        "Umask": 0o077,
        "StandardOutPath": str(log_dir / "automatic-update.log"),
        "StandardErrorPath": str(log_dir / "automatic-update-error.log"),
        "EnvironmentVariables": {"PYTHONUNBUFFERED": "1"},
    }


def _launchctl(*arguments: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["launchctl", *arguments],
        check=check,
        capture_output=True,
        text=True,
        timeout=20,
    )


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
        _launchctl("bootout", domain, str(target), check=False)
        _launchctl("enable", f"{domain}/{LABEL}", check=False)
        _launchctl("bootstrap", domain, str(target))
    return {"installed": True, "enabled": True, "path": str(target)}


def disable(*, home: Path | None = None) -> dict:
    target = launch_agent_path(home)
    domain = f"gui/{os.getuid()}"
    _launchctl("disable", f"{domain}/{LABEL}", check=False)
    _launchctl("bootout", domain, str(target), check=False)
    return {"installed": target.is_file(), "enabled": False, "path": str(target)}


def enable(data_dir: Path, *, home: Path | None = None) -> dict:
    target = launch_agent_path(home)
    return install(data_dir, home=home) if not target.is_file() else _enable_existing(target)


def _enable_existing(target: Path) -> dict:
    domain = f"gui/{os.getuid()}"
    _launchctl("enable", f"{domain}/{LABEL}", check=False)
    _launchctl("bootstrap", domain, str(target))
    return {"installed": True, "enabled": True, "path": str(target)}


def uninstall(*, home: Path | None = None) -> dict:
    target = launch_agent_path(home)
    disable(home=home)
    target.unlink(missing_ok=True)
    return {"installed": False, "enabled": False, "path": str(target)}


def _latest_automatic(data_dir: Path) -> dict | None:
    with reader(data_dir) as c:
        exists = c.execute("SELECT 1 FROM sqlite_master WHERE name='automation_events'").fetchone()
        row = (
            c.execute(
                """SELECT summary_json,created_at FROM automation_events
                   WHERE trigger='scheduled' AND stage='finished' ORDER BY id DESC LIMIT 1"""
            ).fetchone()
            if exists
            else None
        )
    return (json.loads(row[0]) | {"recorded_at": row[1]}) if row else None


def _event(data_dir: Path, run_id: str, stage: str, summary: dict) -> None:
    with NutritionRepository(data_dir) as repo, repo.connection as c:
        c.execute(
            "INSERT INTO automation_events(run_id,trigger,stage,summary_json,created_at) VALUES (?,'scheduled',?,?,?)",
            (run_id, stage, json.dumps(summary, sort_keys=True), _now()),
        )


def run_scheduled_update(data_dir: Path, *, now: datetime | None = None, runner=None) -> dict:
    """Run at most once per local date after 10:00, including login catch-up."""
    from .update import run_update

    current = now or datetime.now().astimezone()
    run_id = str(uuid4())
    latest = _latest_automatic(data_dir)
    latest_success_date = (
        str(latest.get("completed_at", ""))[:10]
        if latest and latest.get("status") in {"complete", "complete_with_unresolved"}
        else None
    )
    if current.timetz().replace(tzinfo=None) < time(SCHEDULE_HOUR):
        result = {"status": "not_due", "completed_at": current.isoformat()}
        _event(data_dir, run_id, "skipped", result)
        return result
    if latest_success_date == current.date().isoformat():
        result = {"status": "already_completed_today", "completed_at": current.isoformat()}
        _event(data_dir, run_id, "skipped", result)
        return result
    _event(data_dir, run_id, "started", {"scheduled_for": current.date().isoformat()})
    operation = runner or run_update
    result = operation(data_dir, trigger="scheduled", publish=True)
    result = dict(result) | {"completed_at": current.isoformat()}
    _event(data_dir, run_id, "finished", result)
    return result


def status(data_dir: Path, *, home: Path | None = None, now: datetime | None = None) -> dict:
    target = launch_agent_path(home)
    domain_target = f"gui/{os.getuid()}/{LABEL}"
    loaded = False
    if target.is_file():
        try:
            loaded = _launchctl("print", domain_target, check=False).returncode == 0
        except (OSError, subprocess.SubprocessError):
            loaded = False
    latest = _latest_automatic(data_dir)
    current = now or datetime.now().astimezone()
    today_at_ten = current.replace(hour=SCHEDULE_HOUR, minute=0, second=0, microsecond=0)
    latest_success_today = bool(
        latest
        and latest.get("status") in {"complete", "complete_with_unresolved"}
        and str(latest.get("completed_at", ""))[:10] == current.date().isoformat()
    )
    if current < today_at_ten:
        next_run = today_at_ten
    elif latest_success_today:
        next_run = today_at_ten + timedelta(days=1)
    else:
        next_run = current
    return {
        "installed": target.is_file(),
        "enabled": loaded,
        "path": str(target),
        "last_automatic": latest,
        "next_expected_run": next_run.isoformat(),
        "catch_up_due": current >= today_at_ten and not latest_success_today,
    }


def main(argv=None):
    from .sync_cli import default_data_dir

    parser = argparse.ArgumentParser(description="Manage the local daily nutrition update")
    parser.add_argument("action", choices=("install", "status", "enable", "disable", "uninstall", "run"))
    parser.add_argument("--data-dir", type=Path, default=default_data_dir())
    args = parser.parse_args(argv)
    actions = {
        "install": lambda: install(args.data_dir),
        "status": lambda: status(args.data_dir),
        "enable": lambda: enable(args.data_dir),
        "disable": disable,
        "uninstall": uninstall,
        "run": lambda: run_scheduled_update(args.data_dir),
    }
    result = actions[args.action]()
    print(json.dumps(result, allow_nan=False))
    return 0 if result.get("status") not in {"failed", "reconnect_required"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
