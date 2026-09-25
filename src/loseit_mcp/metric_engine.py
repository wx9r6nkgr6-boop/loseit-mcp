"""Reusable, UI-independent nutrition periods, definitions and status results."""

from __future__ import annotations

import sqlite3
import statistics
from collections import defaultdict
from contextlib import closing
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .food_patterns import added_sugar_period, read_pattern_period
from .repository import SCHEMA_VERSION
from .resolved import combined_values_for_occurrence

TIMEZONE = ZoneInfo("America/New_York")
PERIODS = ("current_week", "last_week", "rolling_30")
LABELS = {
    "excellent": "Excellent", "on_track": "On Track",
    "slightly_off_track": "Slightly Off Track", "needs_attention": "Needs Attention",
    "significantly_off_track": "Significantly Off Track",
    "informational": "Informational", "insufficient_data": "Insufficient data",
}


@dataclass(frozen=True)
class MetricDefinition:
    key: str
    name: str
    unit: str
    semantic_type: str
    aggregation: str
    target: float | None = None
    minimum: float | None = None
    maximum: float | None = None
    prominent_order: int | None = None
    evidence: str = "NUTRITION_METRICS_AND_FOOD_PATTERNS_SPEC.md, 2026-09-25"


METRICS = (
    MetricDefinition("protein_g", "Protein", "g/day", "minimum", "daily_mean", 150, 130, prominent_order=1),
    MetricDefinition("fiber_g", "Fiber", "g/day", "minimum", "daily_mean", 28, prominent_order=2),
    MetricDefinition("sugar_g", "Total Sugar", "g/day", "informational", "daily_mean", prominent_order=3),
    MetricDefinition("added_sugar_g", "Added Sugar", "g/day", "maximum", "daily_mean", maximum=50),
    MetricDefinition("saturated_fat_g", "Saturated Fat", "% kcal", "maximum", "energy_share", maximum=10),
    MetricDefinition("sodium_mg", "Sodium", "mg/day", "maximum", "daily_mean", maximum=2300),
    MetricDefinition("total_fat_g", "Total Fat", "% kcal", "target_range", "energy_share", minimum=20, maximum=35),
    MetricDefinition("carb_g", "Carbohydrates", "% kcal", "target_range", "energy_share", minimum=45, maximum=65),
    MetricDefinition("cholesterol_mg", "Cholesterol", "mg/day", "informational", "daily_mean"),
    MetricDefinition("calories", "Calories", "kcal/day", "target", "daily_budget"),
    MetricDefinition("weight", "Weight Trend", "source unit", "trend", "rolling_weight"),
)
BY_KEY = {definition.key: definition for definition in METRICS}


def prominent_keys(definitions: list[dict] | tuple[MetricDefinition, ...]) -> list[str]:
    """Resolve default pins by stable keys; callers may later supply any pin count."""
    def pair(definition):
        return ((definition.get("key"), definition.get("prominent_order"))
                if isinstance(definition, dict)
                else (definition.key, definition.prominent_order))

    ordered = [pair(definition) for definition in definitions]
    return [key for key, _ in sorted((entry for entry in ordered if entry[1] is not None),
                                     key=lambda item: item[1])]


def period_bounds(period: str, today: date) -> tuple[date, date, date, date]:
    """Selected start/end and comparable previous start/end, inclusive."""
    if period == "current_week":
        start = today - timedelta(days=today.weekday())
        prior = start - timedelta(days=7)
        return start, today, prior, prior + timedelta(days=today.weekday())
    if period == "last_week":
        end = today - timedelta(days=today.weekday() + 1)
        start = end - timedelta(days=6)
        return start, end, start - timedelta(days=7), end - timedelta(days=7)
    if period == "rolling_30":
        start = today - timedelta(days=29)
        return start, today, start - timedelta(days=30), start - timedelta(days=1)
    raise ValueError("Unsupported nutrition period")


def status_for(key: str, value: float | None, *, reportable: bool = True) -> str:
    """The specification's exact, metric-aware UX bands."""
    if not reportable or value is None:
        return "insufficient_data"
    if key == "protein_g":
        return ("excellent" if value >= 150 else "on_track" if value >= 130
                else "slightly_off_track" if value >= 115 else "needs_attention"
                if value >= 95 else "significantly_off_track")
    if key == "fiber_g":
        ratio = value / 28
        return ("excellent" if ratio >= 1 else "on_track" if ratio >= .9
                else "slightly_off_track" if ratio >= .75 else "needs_attention"
                if ratio >= .5 else "significantly_off_track")
    if key in {"sodium_mg", "added_sugar_g", "saturated_fat_g"}:
        limit = BY_KEY[key].maximum
        assert limit is not None
        ratio = value / limit
        return ("excellent" if ratio <= .8 else "on_track" if ratio <= 1
                else "slightly_off_track" if ratio <= 1.1 else "needs_attention"
                if ratio <= 1.25 else "significantly_off_track")
    if key == "calories":
        delta = abs(value)
        return ("on_track" if delta <= 5 else "slightly_off_track" if delta <= 10
                else "needs_attention" if delta <= 20 else "significantly_off_track")
    return "informational"


def _connection(data_dir: Path) -> sqlite3.Connection:
    path = (data_dir.expanduser() / "nutrition.sqlite3").resolve()
    c = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA query_only=ON")
    return c


def _completion(c: sqlite3.Connection) -> dict[str, str]:
    return {
        row["source_date"]: row["status"]
        for row in c.execute("SELECT source_date,status FROM day_completion_observations ORDER BY id")
    }


def _allowances(c: sqlite3.Connection) -> dict[str, float]:
    if not c.execute("SELECT 1 FROM sqlite_master WHERE name='daily_allowance_observations'").fetchone():
        return {}
    return {
        row["source_date"]: row["final_daily_allowance"]
        for row in c.execute(
            """SELECT source_date,final_daily_allowance FROM daily_allowance_observations
               WHERE provenance='loseit_validated_final' ORDER BY id"""
        )
    }


def _period_values(c: sqlite3.Connection, start: date, end: date, today: date) -> dict:
    days = defaultdict(lambda: {"values": defaultdict(float), "present": defaultdict(int),
                               "missing": defaultdict(int), "provenance": defaultdict(int),
                               "known_source_calories": defaultdict(float),
                               "source_calories": 0.0,
                               "occurrences": 0})
    rows = c.execute(
        "SELECT * FROM food_occurrences WHERE is_current=1 AND source_date BETWEEN ? AND ? ORDER BY id",
        (start.isoformat(), end.isoformat()),
    ).fetchall()
    for raw in rows:
        row = dict(raw)
        day = days[row["source_date"]]
        day["occurrences"] += 1
        resolved = combined_values_for_occurrence(c, row)
        calorie_item = resolved.get("calories")
        source_calories = (calorie_item["value"] if calorie_item and
                           calorie_item["provenance"] == "source" else 0.0)
        day["source_calories"] += source_calories
        for definition in METRICS:
            key = definition.key
            if key in {"added_sugar_g", "weight"}:
                continue
            item = resolved.get(key)
            if item:
                day["values"][key] += item["value"]
                day["present"][key] += 1
                day["provenance"][(key, item["provenance"])] += 1
                day["known_source_calories"][key] += source_calories
            else:
                day["missing"][key] += 1
    completion = _completion(c)
    eligible = []
    provisional = []
    current_day_partial = False
    all_dates = [start + timedelta(days=i) for i in range((end - start).days + 1)]
    for day in all_dates:
        iso = day.isoformat()
        state = completion.get(iso, "unknown")
        has_entries = iso in days
        if day == today and state != "complete":
            current_day_partial = has_entries
            continue
        if state in {"incomplete", "not_complete"}:
            continue
        if has_entries or state == "complete":
            eligible.append(iso)
            if state != "complete":
                provisional.append(iso)
    results = {}
    allowances = _allowances(c)
    total_occurrences = sum(days[day]["occurrences"] for day in eligible)
    for definition in METRICS:
        key = definition.key
        if key in {"added_sugar_g", "weight"}:
            continue
        present = sum(days[day]["present"][key] for day in eligible)
        missing = total_occurrences - present
        total = sum(days[day]["values"][key] for day in eligible)
        coverage = 100 * present / total_occurrences if total_occurrences else None
        source_kcal = sum(days[day]["source_calories"] for day in eligible)
        covered_kcal = sum(days[day]["known_source_calories"][key] for day in eligible)
        calorie_coverage = 100 * covered_kcal / source_kcal if source_kcal else None
        quality_ok = bool(eligible and (missing == 0 or
                          (coverage is not None and coverage >= 95 and
                           calorie_coverage is not None and calorie_coverage >= 95)))
        if key == "calories":
            matched = [day for day in eligible if day in allowances and days[day]["present"][key] == days[day]["occurrences"]]
            budget = sum(allowances[day] for day in matched) if matched else None
            observed = sum(days[day]["values"][key] for day in matched)
            deviation = 100 * (observed - budget) / budget if budget else None
            value = total / len(eligible) if eligible else None
            reportable = quality_ok and len(matched) == len(eligible)
            status = status_for(key, deviation, reportable=reportable) if budget is not None else "informational"
        elif definition.aggregation == "energy_share":
            calories = sum(days[day]["values"]["calories"] for day in eligible)
            factor = 9 if key == "total_fat_g" or key == "saturated_fat_g" else 4
            value = 100 * factor * total / calories if calories > 0 else None
            reportable = quality_ok and calories > 0
            status = status_for(key, value, reportable=reportable)
            budget = deviation = None
        else:
            value = total / len(eligible) if eligible else None
            reportable = quality_ok
            status = status_for(key, value, reportable=reportable)
            budget = deviation = None
        source_count = sum(days[day]["provenance"][(key, "source")] for day in eligible)
        estimated_count = sum(count for day in eligible
                              for (nutrient, name), count in days[day]["provenance"].items()
                              if nutrient == key and name != "source")
        results[key] = {
            "value": round(value, 2) if value is not None else None,
            "period_total": round(total, 2) if eligible else None,
            "status": status, "status_label": LABELS[status],
            "reportable": reportable, "coverage_pct": round(coverage, 1) if coverage is not None else None,
            "source_calorie_coverage_pct": round(calorie_coverage, 1) if calorie_coverage is not None else None,
            "missing_occurrences": missing,
            "provisional": bool(provisional),
            "source_occurrences": source_count,
            "estimated_occurrences": estimated_count,
            "quality": "Incomplete" if not quality_ok else
                       "Mostly reliable" if estimated_count else "Verified",
            "budget_total": round(budget, 2) if budget is not None else None,
            "budget_deviation_pct": round(deviation, 2) if deviation is not None else None,
            "budget_days": len(matched) if key == "calories" else None,
            "unavailable_reason": ("Actual Lose It allowance unavailable" if budget is None
                                   else "Some eligible days lack actual Lose It allowance")
                                  if key == "calories" and not reportable else None,
            "daily_values": {
                day: round(days[day]["values"][key], 2)
                for day in eligible if days[day]["present"][key]
            },
            "period_reference_total": (round(definition.target * len(eligible), 2)
                                       if key == "protein_g" and eligible else
                                       round(definition.maximum * len(eligible), 2)
                                       if key == "sodium_mg" and eligible else
                                       round(definition.target * len(eligible), 2)
                                       if key == "fiber_g" and eligible else None),
        }
    added = added_sugar_period(c, start, end, eligible_dates=set(eligible))
    value = added["known_total_g"] / len(eligible) if added["known_total_g"] is not None and eligible else None
    reportable = added["reportable"] and bool(eligible)
    added_status = status_for("added_sugar_g", value, reportable=reportable)
    results["added_sugar_g"] = {
        "value": round(value, 2) if value is not None and reportable else None,
        "known_total_g": added["known_total_g"], "status": added_status,
        "status_label": LABELS[added_status], "reportable": reportable,
        "coverage_pct": added["occurrence_coverage_pct"],
        "source_calorie_coverage_pct": added["source_calorie_coverage_pct"],
        "quality": added["quality"],
        "unavailable_reason": None if reportable else "Added-sugar evidence below coverage gate",
    }
    weight_start = end - timedelta(days=6)
    weights = [dict(row) for row in c.execute(
        "SELECT source_date,weight,unit FROM weight_observations WHERE source_date BETWEEN ? AND ? ORDER BY source_date",
        (weight_start.isoformat(), end.isoformat()),
    )]
    unit_set = {row["unit"] for row in weights}
    weight_value = statistics.mean(row["weight"] for row in weights) if weights else None
    period_weights = [dict(row) for row in c.execute(
        "SELECT source_date,weight,unit FROM weight_observations WHERE source_date BETWEEN ? AND ? ORDER BY source_date",
        (start.isoformat(), end.isoformat()),
    )]
    trend = None
    if len(period_weights) >= 3 and len({row["unit"] for row in period_weights}) == 1 and period_weights[0]["unit"]:
        x = [(date.fromisoformat(row["source_date"]) - start).days for row in period_weights]
        y = [row["weight"] for row in period_weights]
        mean_x = statistics.mean(x)
        denominator = sum((point - mean_x) ** 2 for point in x)
        if denominator:
            trend = 7 * sum((point - mean_x) * (value - statistics.mean(y))
                            for point, value in zip(x, y, strict=True)) / denominator
    results["weight"] = {
        "value": round(weight_value, 2) if weight_value is not None else None,
        "observations": len(weights), "unit": next(iter(unit_set)) if len(unit_set) == 1 else None,
        "status": "informational" if len(weights) >= 3 else "insufficient_data",
        "status_label": "Informational" if len(weights) >= 3 else "Insufficient data",
        "reportable": len(weights) >= 3, "unavailable_reason": "Weight unit unconfirmed" if None in unit_set else None,
        "quality": "Incomplete" if len(weights) < 3 or None in unit_set else "Mostly reliable",
        "trend_per_week": round(trend, 2) if trend is not None else None,
    }
    return {
        "start": start.isoformat(), "end": end.isoformat(),
        "calendar_days": len(all_dates), "eligible_days": len(eligible),
        "eligible_dates": eligible, "provisional_dates": provisional,
        "current_day_partial": current_day_partial,
        "missing_days": [day.isoformat() for day in all_dates if day.isoformat() not in eligible],
        "nutrients": results, "food_patterns": read_pattern_period(c, start, end, eligible_dates=set(eligible)),
    }


def read_metric_dashboard(data_dir: Path, period: str, *, today: date | None = None) -> dict:
    """Stable JSON-ready boundary for Streamlit, static output and future clients."""
    from datetime import datetime

    today = today or datetime.now(TIMEZONE).date()
    start, end, prior_start, prior_end = period_bounds(period, today)
    with closing(_connection(data_dir)) as c:
        version = c.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
        if version != SCHEMA_VERSION:
            raise ValueError("Metric engine requires current nutrition schema")
        current = _period_values(c, start, end, today)
        previous = _period_values(c, prior_start, prior_end, today)
    for key, value in current["nutrients"].items():
        value["previous_comparable_value"] = previous["nutrients"][key]["value"]
    for key, value in current["food_patterns"].items():
        previous_value = previous["food_patterns"][key]
        value["previous_meaningful_occurrences"] = previous_value.get("meaningful_occurrences")
        if key == "plant_variety":
            value["previous_distinct_plants"] = previous_value["distinct_plants"]
    return {
        "schema_version": 1, "period": period, "today": today.isoformat(),
        "timezone": TIMEZONE.key, "definition_version": "2026-09-25-v1",
        "definitions": [definition.__dict__ for definition in METRICS],
        "current": current,
        "previous_period": {"start": prior_start.isoformat(), "end": prior_end.isoformat(),
                            "eligible_days": previous["eligible_days"]},
    }
