"""Local dashboard preferences and semantic, reusable metric-card models."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

DEFAULT_PINS = ("protein_g", "fiber_g", "sugar_g")
PREFERENCE_FILE = "dashboard_preferences.json"
PREFERENCE_VERSION = 1


def _keys(definitions: list[dict]) -> set[str]:
    return {item["key"] for item in definitions}


def normalize_preferences(value: object, definitions: list[dict]) -> dict:
    """Ignore retired IDs and malformed local data; expose new metrics unpinned."""
    allowed = _keys(definitions)
    fallback = {"version": PREFERENCE_VERSION,
                "pinned": [key for key in DEFAULT_PINS if key in allowed], "hidden": []}
    if not isinstance(value, dict) or value.get("version") != PREFERENCE_VERSION:
        return fallback
    pinned = value.get("pinned")
    hidden = value.get("hidden")
    if not isinstance(pinned, list) or not isinstance(hidden, list):
        return fallback
    if not all(isinstance(key, str) for key in pinned + hidden):
        return fallback
    seen = set()
    valid_pins = []
    for key in pinned:
        if key in allowed and key not in seen:
            valid_pins.append(key)
            seen.add(key)
    valid_hidden = []
    for key in hidden:
        if key in allowed and key not in seen and key not in valid_hidden:
            valid_hidden.append(key)
    return {"version": PREFERENCE_VERSION, "pinned": valid_pins, "hidden": valid_hidden}


def load_preferences(data_dir: Path, definitions: list[dict]) -> dict:
    path = Path(data_dir).expanduser() / PREFERENCE_FILE
    try:
        if path.stat().st_size > 8192:
            return normalize_preferences(None, definitions)
        return normalize_preferences(json.loads(path.read_text(encoding="utf-8")), definitions)
    except (OSError, UnicodeError, ValueError, TypeError):
        return normalize_preferences(None, definitions)


def save_preferences(data_dir: Path, preferences: dict, definitions: list[dict]) -> dict:
    data = normalize_preferences(preferences, definitions)
    directory = Path(data_dir).expanduser().resolve()
    if not directory.is_dir():
        raise ValueError("Existing nutrition data directory required")
    target = directory / PREFERENCE_FILE
    fd, temporary = tempfile.mkstemp(prefix=".dashboard_preferences.", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, target)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return data


def change_preference(preferences: dict, action: str, key: str,
                      definitions: list[dict]) -> dict:
    if key not in _keys(definitions):
        raise ValueError("Unknown metric")
    current = normalize_preferences(preferences, definitions)
    pinned, hidden = current["pinned"][:], current["hidden"][:]
    if action == "pin":
        if key in hidden:
            hidden.remove(key)
        if key not in pinned:
            pinned.append(key)
    elif action == "unpin":
        if key in pinned:
            pinned.remove(key)
    elif action == "hide":
        if key in pinned:
            pinned.remove(key)
        if key not in hidden:
            hidden.append(key)
    elif action == "restore":
        if key in hidden:
            hidden.remove(key)
    elif action in {"up", "down"}:
        if key in pinned:
            index = pinned.index(key)
            destination = index + (-1 if action == "up" else 1)
            if 0 <= destination < len(pinned):
                pinned[index], pinned[destination] = pinned[destination], pinned[index]
    else:
        raise ValueError("Unsupported preference action")
    return {"version": PREFERENCE_VERSION, "pinned": pinned, "hidden": hidden}


def _number(value: float | None, decimals: int = 1) -> str:
    if value is None:
        return "Unavailable"
    return f"{value:,.{decimals}f}"


def card_model(definition: dict, result: dict) -> dict:
    """Goal text and progress semantics, independent of either renderer."""
    key = definition["key"]
    value = result.get("value")
    semantic = definition["semantic_type"]
    unit = definition["unit"]
    model = {
        "key": key, "name": definition["name"],
        "value_text": _number(value) + (" " + unit if value is not None else ""),
        "status": result["status_label"], "quality": result["quality"],
        "goal_text": None, "progress_text": None,
        "progress_kind": None, "progress_fraction": None,
        "previous_text": None,
    }
    if key == "weight" and value is not None:
        model["value_text"] = (_number(value) + " " + result["unit"]
                               if result.get("unit") else _number(value) + " · unit unconfirmed")
    previous = result.get("previous_comparable_value")
    if previous is not None:
        model["previous_text"] = f"Previous comparable: {_number(previous)} {unit}"
    # Macro shares are explicitly informational despite contextual AMDR ranges.
    if value is None or not result.get("reportable") or key in {"total_fat_g", "carb_g"}:
        return model
    if semantic == "minimum":
        goal = definition["target"]
        if goal is None or goal <= 0:
            return model
        model["goal_text"] = f"Goal: {_number(goal, 0)} {unit}"
        model["progress_text"] = f"{100 * value / goal:.0f}% of {_number(goal, 0)} {unit} target"
        model["progress_kind"] = "adequacy"
        model["progress_fraction"] = min(max(value / goal, 0), 1)
        if key == "protein_g":
            model["goal_text"] += " · On Track from 130 g/day"
    elif semantic == "maximum":
        limit = definition["maximum"]
        if limit is None or limit <= 0:
            return model
        reference = "<" if key in {"saturated_fat_g", "added_sugar_g"} else "≤"
        model["goal_text"] = (f"Goal: {reference} {_number(limit, 0)}{unit}" if
                              unit.startswith("%") else f"Goal: {reference} {_number(limit, 0)} {unit}")
        model["progress_kind"] = "limit_over" if value > limit else "limit"
        model["progress_fraction"] = min(max(value / limit, 0), 1)
        model["progress_text"] = (f"{_number(value - limit)} percentage points over limit"
                                  if value > limit and unit.startswith("%") else
                                  f"{_number(value - limit)} {unit} over daily limit"
                                  if value > limit else f"{100 * value / limit:.0f}% of limit used")
    elif semantic == "target_range" and definition.get("minimum") is not None and definition.get("maximum") is not None:
        lower, upper = definition["minimum"], definition["maximum"]
        model["goal_text"] = f"Reference range: {_number(lower, 0)}–{_number(upper, 0)} {unit}"
        model["progress_text"] = "Within range" if lower <= value <= upper else "Outside range"
        model["progress_kind"] = "range"
        model["progress_fraction"] = min(max(value / max(upper * 1.5, 1), 0), 1)
    elif semantic == "target" and key == "calories" and result.get("budget_total"):
        days = result.get("budget_days") or 0
        if days:
            average = result["budget_total"] / days
            model["goal_text"] = f"Actual Lose It allowance: {_number(average, 0)} kcal/day"
            model["progress_text"] = f"{100 * value / average:.0f}% of allowance used"
            model["progress_kind"] = "limit_over" if value > average else "limit"
            model["progress_fraction"] = min(max(value / average, 0), 1)
    return model


def attention_keys(view: dict, preferences: dict, *, limit: int = 2) -> list[str]:
    current = view["current"]
    if current["eligible_days"] < 2:
        return []
    pinned = set(preferences["pinned"])
    hidden = set(preferences["hidden"])
    priorities = {"significantly_off_track": 0, "needs_attention": 1}
    choices = []
    for definition in view["definitions"]:
        key = definition["key"]
        result = current["nutrients"][key]
        if (key in pinned or key in hidden or result["status"] not in priorities
                or not result.get("reportable") or result["quality"] == "Incomplete"):
            continue
        choices.append((priorities[result["status"]], key))
    return [key for _, key in sorted(choices)[:limit]]
