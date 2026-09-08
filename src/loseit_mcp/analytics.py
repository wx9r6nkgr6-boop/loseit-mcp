"""UI-independent, read-only calendar analytics with explicit partial-data semantics."""

from __future__ import annotations

import calendar
import json
import sqlite3
import statistics
from collections import Counter, defaultdict
from datetime import date, timedelta
from pathlib import Path

from .repository import SCHEMA_VERSION, STANDARD_NUTRIENTS
from .research_queue import EXACT_MATCHES, _conflicts, _finite, _groups, _queue, _table

NUTRIENTS = STANDARD_NUTRIENTS
DAILY_FIELDS = [
    "source_reported",
    "estimated_or_enriched",
    "combined_usable",
    "source_count",
    "estimated_count",
    "missing_count",
    "total_occurrences",
]


def period_dates(*, period="last7", start=None, end=None, today=None):
    today = today or date.today()  # noqa: DTZ011 - intentionally local calendar date
    if start or end:
        if not start or not end or end < start:
            raise ValueError("Custom period requires ordered start and end dates")
        return start, end
    if period.startswith("last") and period[4:].isdigit():
        days = int(period[4:])
        if not 1 <= days <= 3660:
            raise ValueError("Analytics days must be between 1 and 3660")
        return today - timedelta(days=days - 1), today
    if period == "week":
        return today - timedelta(days=today.weekday()), today
    if period == "month":
        return today.replace(day=1), today
    if period == "ytd":
        return today.replace(month=1, day=1), today
    raise ValueError("Unknown analytics period")


def _days(start, end):
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


def _sum(values):
    found = [v for v in values if v is not None]
    return round(sum(found), 6) if found else None


def _avg(values):
    found = [v for v in values if v is not None]
    return round(statistics.mean(found), 6) if found else None


def _pct(n, d):
    return round(100 * n / d, 3) if d else None


def _metric(rows, nutrient):
    source = [r["source_nutrients"].get(nutrient) for r in rows]
    # Enrichment is gap-fill only, never added on top of a source value.
    enriched = [
        r["estimates"].get(nutrient) if s is None else None
        for r, s in zip(rows, source, strict=True)
    ]
    combined = [s if s is not None else e for s, e in zip(source, enriched, strict=True)]
    sc, ec = sum(v is not None for v in source), sum(v is not None for v in enriched)
    return [_sum(source), _sum(enriched), _sum(combined), sc, ec, len(rows) - sc - ec, len(rows)]


def _prepare(c):
    groups = _groups(c)
    links = {
        r["occurrence_id"]: dict(r)
        for r in c.execute(
            "SELECT l.occurrence_id,l.match_method,v.* FROM occurrence_reference_links l JOIN latest_enrichment_versions v ON v.reference_id=l.reference_id"
        )
    }
    reviews = (
        {
            r["enrichment_version_id"]: dict(r)
            for r in c.execute("SELECT * FROM enrichment_review_contexts")
        }
        if _table(c, "enrichment_review_contexts")
        else {}
    )
    estimates = defaultdict(list)
    for r in c.execute("SELECT * FROM estimated_nutrients"):
        estimates[r["enrichment_version_id"]].append(dict(r))
    records = []
    for group in groups:
        context = group["context"]
        for row in group["rows"]:
            row["estimates"] = {}
            row["quality_flags"] = []
            n = row["source_nutrients"]
            if any(v < 0 for v in n.values()):
                row["quality_flags"].append("negative_source_nutrient")
            known_macro_energy = sum(
                n.get(k, 0) * factor
                for k, factor in (("protein_g", 4), ("carb_g", 4), ("total_fat_g", 9))
            )
            if "calories" in n and known_macro_energy - n["calories"] > max(
                100, abs(n["calories"]) * 0.4
            ):
                row["quality_flags"].append("known_macro_energy_exceeds_calories_review_only")
            for sub, total in (
                ("sugar_g", "carb_g"),
                ("fiber_g", "carb_g"),
                ("saturated_fat_g", "total_fat_g"),
            ):
                if sub in n and total in n and n[sub] > n[total] + 1:
                    row["quality_flags"].append(f"{sub}_exceeds_{total}")
            if all(k in n for k in ("calories", "protein_g", "carb_g", "total_fat_g")):
                macro = 4 * n["protein_g"] + 4 * n["carb_g"] + 9 * n["total_fat_g"]
                if abs(macro - n["calories"]) > max(100, abs(n["calories"]) * 0.4):
                    row["quality_flags"].append("calorie_macro_discrepancy_review_only")
            if _conflicts(context):
                row["quality_flags"].append("conflicting_source_context")
            link = links.get(row["id"])
            if link:
                review = reviews.get(link["id"], {})
                past = json.loads(review.get("source_context_json", "[]"))
                trusted = (
                    link["manually_reviewed"] == 1
                    and link["confidence"] == "high"
                    and link["match_type"] != "insufficient_information"
                    and (
                        link["match_type"] in EXACT_MATCHES
                        or link["match_method"] == "manually_confirmed_alias"
                    )
                    and link["match_method"]
                    in {"source_food_id", "exact_normalized_name_brand", "manually_confirmed_alias"}
                    and not _conflicts(context + past)
                )
                basis = json.loads(review.get("nutrition_basis_json", "{}"))
                scaling = basis.get("occurrence_scaling", {})
                factor = None
                # A reviewed explicit mapping is required. Never infer from string similarity.
                if (
                    trusted
                    and scaling.get("method") == "logged_amount"
                    and scaling.get("unit") == row["unit"] == basis.get("unit")
                    and _finite(row["amount"])
                    and row["amount"] >= 0
                    and _finite(basis.get("amount"))
                    and basis["amount"] > 0
                ):
                    factor = row["amount"] / basis["amount"]
                if factor is None:
                    row["quality_flags"].append(
                        "enrichment_unreviewed_or_portion_mapping_unavailable"
                    )
                else:
                    for estimate in estimates[link["id"]]:
                        if estimate["nutrient"] in NUTRIENTS and _finite(
                            estimate["estimated_value"]
                        ):
                            row["estimates"][estimate["nutrient"]] = (
                                estimate["estimated_value"] * factor
                            )
            records.append(row)
    return records


def _weight(rows, start, end):
    selected = [
        r
        for r in rows
        if start.isoformat() <= r["source_date"] <= end.isoformat() and _finite(r["weight"])
    ]
    units = {r["unit"] for r in selected}
    values = [r["weight"] for r in selected]
    consistent = len(units) == 1 and None not in units and "" not in units
    daily = []
    for r in selected:
        day = date.fromisoformat(r["source_date"])
        point = {"date": r["source_date"], "weight": r["weight"], "unit": r["unit"]}
        for window, minimum in ((7, 3), (30, 7)):
            observed = [
                x["weight"]
                for x in rows
                if (day - timedelta(days=window - 1)).isoformat()
                <= x["source_date"]
                <= day.isoformat()
                and x["unit"] == r["unit"]
                and _finite(x["weight"])
            ]
            point[f"rolling_{window}"] = (
                _avg(observed) if len(observed) >= minimum and r["unit"] else None
            )
            point[f"rolling_{window}_count"] = len(observed)
        daily.append(point)
    return {
        "unit": next(iter(units)) if consistent else None,
        "mixed_or_unknown_units": bool(values) and not consistent,
        "observation_count": len(values),
        "first": daily[0] if daily else None,
        "latest": daily[-1] if daily else None,
        "change": round(values[-1] - values[0], 6) if consistent and len(values) > 1 else None,
        "average": _avg(values) if consistent else None,
        "min": min(values) if values and consistent else None,
        "max": max(values) if values and consistent else None,
        "daily": daily,
    }


def _summary(rows, days):
    logged = {r["source_date"] for r in rows}
    by_day = defaultdict(list)
    for row in rows:
        by_day[row["source_date"]].append(row)
    nutrients = {}
    for n in NUTRIENTS:
        m = _metric(rows, n)
        daily = [_metric(values, n) for values in by_day.values()]
        complete_values = [v[2] for v in daily if v[5] == 0]
        nutrients[n] = dict(zip(DAILY_FIELDS, m, strict=True)) | {
            "average_per_complete_logged_day": _avg(complete_values),
            "complete_logged_days": len(complete_values),
            "source_coverage_pct": _pct(m[3], m[6]),
            "combined_coverage_pct": _pct(m[3] + m[4], m[6]),
            "average_per_logged_day": m[2] / len(logged) if logged and m[5] == 0 else None,
            "average_per_calendar_day": m[2] / len(days)
            if logged and m[5] == 0 and len(logged) == len(days)
            else None,
            "recorded_contribution_per_calendar_day": m[2] / len(days)
            if m[2] is not None and days
            else None,
            "complete_for_logged_days": bool(logged) and m[5] == 0,
        }
    return {
        "logged_days": len(logged),
        "calendar_days": len(days),
        "missing_days": len(days) - len(logged),
        "nutrients": nutrients,
    }


def _meals(rows):
    groups = defaultdict(list)
    for row in rows:
        groups[str(row["meal"]) if row["meal"] is not None else "unclassified"].append(row)
    for name in ("breakfast", "lunch", "dinner", "snacks"):
        groups.setdefault(name, [])
    result = {}
    for name, values in sorted(groups.items()):
        meal_days = len({r["source_date"] for r in values})
        record = {"logged_meal_days": meal_days, "occurrence_count": len(values)}
        for n in ("calories", "protein_g", "carb_g", "total_fat_g"):
            m, all_m = _metric(values, n), _metric(rows, n)
            daily_shares = []
            for day in sorted({r["source_date"] for r in values}):
                numerator = _metric([r for r in values if r["source_date"] == day], n)
                denominator = _metric([r for r in rows if r["source_date"] == day], n)
                if numerator[2] is not None and not denominator[5]:
                    daily_shares.append(_pct(numerator[2], denominator[2]))
            record[n] = {
                "source_reported": m[0],
                "estimated_or_enriched": m[1],
                "combined_usable": m[2],
                "missing_count": m[5],
                "average_per_logged_meal_day": m[2] / meal_days if meal_days and not m[5] else None,
                "period_share_pct": _pct(m[2], all_m[2])
                if m[2] is not None and not all_m[5]
                else None,
                "mean_share_of_daily_total_pct_on_logged_meal_days": _avg(daily_shares),
            }
        result[name] = record
    return result


def _foods(rows, grouping):
    groups = defaultdict(list)
    for row in rows:
        key = (
            (row["source"], row["source_food_id"])
            if row["source_food_id"] and grouping == "id"
            else (row["source"], row["food_name_normalized"], row["brand_normalized"])
        )
        groups[key].append(row)
    foods = []
    for values in groups.values():
        r = values[-1]
        units = {v["unit"] for v in values}
        quantities = [v["amount"] for v in values]
        foods.append(
            {
                "source_food_id": r["source_food_id"] if grouping == "id" else None,
                "food_name": r["food_name_normalized"],
                "brand": r["brand_normalized"],
                "occurrence_count": len(values),
                "logged_days": len({v["source_date"] for v in values}),
                "average_logged_amount": _avg(quantities)
                if len(units) == 1 and next(iter(units)) and all(_finite(v) for v in quantities)
                else None,
                "unit": next(iter(units)) if len(units) == 1 else None,
                "metrics": {n: _metric(values, n) for n in NUTRIENTS},
                "review_flags": sorted({flag for v in values for flag in v["quality_flags"]}),
            }
        )
    foods.sort(key=lambda f: (-f["occurrence_count"], f["food_name"], f["source_food_id"] or ""))
    ranks = {
        n: sorted(
            [f for f in foods if f["metrics"][n][2] is not None],
            key=lambda f: (-f["metrics"][n][2], f["food_name"]),
        )[:10]
        for n in ("calories", "protein_g", "carb_g", "total_fat_g", "sugar_g", "sodium_mg")
    }
    # Index the food catalog rather than repeating nutrient payloads for every ranking.
    flagged = [f for f in foods if f["review_flags"]]
    catalog = (
        foods[:10]
        + [f for ranking in ranks.values() for f in ranking if f not in foods[:10]]
        + flagged[:20]
    )
    unique = []
    for f in catalog:
        if f not in unique:
            unique.append(f)
    return {
        "grouping": grouping,
        "food_count": len(foods),
        "recurring_food_count": sum(f["occurrence_count"] > 1 for f in foods),
        "manual_review_food_count": len(flagged),
        "catalog": unique,
        "most_frequent": list(range(min(10, len(foods)))),
        "rankings": {n: [unique.index(f) for f in rank] for n, rank in ranks.items()},
    }


def _consistency(daily, protein_target, calorie_min, calorie_max):
    result = {}
    for n in ("calories", "protein_g"):
        values = [
            d["nutrients"][n][2]
            for d in daily
            if d["occurrence_count"] and not d["nutrients"][n][5]
        ]
        mean = _avg(values)
        result[n] = {
            "eligible_complete_days": len(values),
            "population_stddev": statistics.pstdev(values) if len(values) > 1 else None,
            "coefficient_of_variation": statistics.pstdev(values) / mean
            if len(values) > 1 and mean
            else None,
        }
        target = protein_target if n == "protein_g" else calorie_min
        passed = (
            sum(v >= protein_target for v in values)
            if n == "protein_g" and protein_target is not None
            else sum(calorie_min <= v <= calorie_max for v in values)
            if n == "calories" and calorie_min is not None
            else None
        )
        result[n].update(
            target=target,
            target_upper=calorie_max if n == "calories" else None,
            target_days=passed,
            target_pct=_pct(passed, len(values)) if passed is not None else None,
        )
    streak = longest = 0
    for d in daily:
        streak = streak + 1 if d["occurrence_count"] else 0
        longest = max(longest, streak)
    result.update(longest_logged_streak=longest, current_logged_streak=streak)
    return result


def _quality_foods(rows, research):
    groups = defaultdict(list)

    def key(row):
        return row["source_food_id"] or (row["food_name_normalized"], row["brand_normalized"])

    for row in rows:
        groups[key(row)].append(row)
    queue = {f["source_food_id"] or (f["food_name"], f["brand"]): f["status"] for f in research}
    result = []
    for identity, values in groups.items():
        source_missing = [
            n for n in NUTRIENTS if any(n not in r["source_nutrients"] for r in values)
        ]
        missing = [n for n in NUTRIENTS if _metric(values, n)[5]]
        flags = sorted({flag for r in values for flag in r["quality_flags"]})
        if source_missing or flags:
            result.append(
                {
                    "source_food_id": values[-1]["source_food_id"],
                    "food_name": values[-1]["food_name_normalized"],
                    "brand": values[-1]["brand_normalized"],
                    "occurrence_count": len(values),
                    "source_missing": source_missing,
                    "numeric_missing": missing,
                    "filled_by_scaled_enrichment": [n for n in source_missing if n not in missing],
                    "review_flags": flags,
                    "research_queue_status_global": queue.get(identity, "complete"),
                }
            )
    return sorted(
        result, key=lambda r: (-r["occurrence_count"], r["food_name"], r["source_food_id"] or "")
    )


def read_analytics(
    data_dir: Path,
    start: date,
    end: date,
    *,
    compare=True,
    period="custom",
    grouping="id",
    protein_target=None,
    calorie_min=None,
    calorie_max=None,
):
    if end < start or (end - start).days > 3660:
        raise ValueError("Analytics period must be ordered and at most 3661 days")
    if grouping not in {"id", "name"}:
        raise ValueError("Grouping must be id or name")
    if (
        any(
            v is not None and (not _finite(v) or v < 0)
            for v in (protein_target, calorie_min, calorie_max)
        )
        or ((calorie_min is None) != (calorie_max is None))
        or (calorie_min is not None and calorie_min > calorie_max)
    ):
        raise ValueError(
            "Targets must be finite nonnegative values; calorie range requires both ordered bounds"
        )
    path = (data_dir.expanduser() / "nutrition.sqlite3").resolve()
    c = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    try:
        c.execute("PRAGMA query_only=ON")
        c.execute("BEGIN")
        version = c.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
        if version not in range(1, SCHEMA_VERSION + 1):
            raise ValueError("Unsupported analytics database schema")
        records = _prepare(c)
        weights = [
            dict(r)
            for r in c.execute(
                "SELECT source_date,weight,unit FROM weight_observations ORDER BY source_date"
            )
        ]
        queue = _queue(c)
        retrieved = {
            r[0] for r in c.execute("SELECT DISTINCT source_date FROM raw_diary_snapshots")
        }
        return _report(
            records,
            weights,
            queue,
            retrieved,
            start,
            end,
            compare,
            period,
            grouping,
            protein_target,
            calorie_min,
            calorie_max,
        )
    finally:
        c.close()


def _report(
    records,
    weights,
    queue,
    retrieved,
    start,
    end,
    compare,
    period,
    grouping,
    protein_target,
    calorie_min,
    calorie_max,
):
    days = _days(start, end)
    rows = [r for r in records if start.isoformat() <= r["source_date"] <= end.isoformat()]
    by_day = defaultdict(list)
    for row in rows:
        by_day[row["source_date"]].append(row)
    daily = [
        {
            "date": day.isoformat(),
            "occurrence_count": len(by_day[day.isoformat()]),
            "retrieved": day.isoformat() in retrieved,
            "nutrients": {n: _metric(by_day[day.isoformat()], n) for n in NUTRIENTS},
        }
        for day in days
    ]
    summary = _summary(rows, days)

    def subgroup(selected_days):
        strings = {d.isoformat() for d in selected_days}
        selected = [r for r in rows if r["source_date"] in strings]
        return _summary(selected, selected_days) | {
            "meals": _meals(selected),
            "weight": _weight([r for r in weights if r["source_date"] in strings], start, end),
        }

    periods = {}
    for kind in ("week", "month"):
        buckets = defaultdict(list)
        for day in days:
            key = (
                (day - timedelta(days=day.weekday())).isoformat()
                if kind == "week"
                else day.strftime("%Y-%m")
            )
            buckets[key].append(day)
        periods[kind] = [
            {
                "label": label,
                "start": dates[0].isoformat(),
                "end": dates[-1].isoformat(),
                **_summary(
                    [
                        r
                        for r in rows
                        if dates[0].isoformat() <= r["source_date"] <= dates[-1].isoformat()
                    ],
                    dates,
                ),
                "weight": {
                    k: v for k, v in _weight(weights, dates[0], dates[-1]).items() if k != "daily"
                },
            }
            for label, dates in buckets.items()
        ]
    calories = [r["source_nutrients"].get("calories") for r in rows]
    complete_macro_calories = sum(
        r["source_nutrients"].get("calories", 0)
        for r in rows
        if all(
            n in r["source_nutrients"] or n in r["estimates"]
            for n in ("calories", "protein_g", "carb_g", "total_fat_g")
        )
    )
    flags = Counter(flag for r in rows for flag in r["quality_flags"])
    selected_ids = {r["source_food_id"] for r in rows if r["source_food_id"]}
    selected_names = {
        (r["food_name_normalized"], r["brand_normalized"]) for r in rows if not r["source_food_id"]
    }
    relevant = [
        f
        for f in queue["foods"]
        if f["source_food_id"] in selected_ids
        or (not f["source_food_id"] and (f["food_name"], f["brand"]) in selected_names)
    ]
    food_report = _foods(rows, grouping)
    report = {
        "schema_version": 1,
        "local_only": True,
        "period": {
            "start": start.isoformat(),
            "end": end.isoformat(),
            "kind": period,
            "calendar_basis": "stored local source dates; local machine date for relative windows",
        },
        "daily_metric_fields": DAILY_FIELDS,
        "daily_metrics": daily,
        "logging_completeness": {
            k: summary[k] for k in ("logged_days", "calendar_days", "missing_days")
        },
        "period_averages": summary["nutrients"],
        "nutrient_coverage": {
            n: {
                k: v
                for k, v in m.items()
                if k.endswith("count") or "coverage" in k or k == "total_occurrences"
            }
            for n, m in summary["nutrients"].items()
        },
        "weight_summary": _weight(weights, start, end),
        "meal_summary": _meals(rows),
        "food_contributors": food_report,
        "calendar_summaries": periods,
        "consistency": _consistency(daily, protein_target, calorie_min, calorie_max),
        "weekday_weekend": {
            label: subgroup([d for d in days if (d.weekday() >= 5) == weekend])
            for label, weekend in (("weekday", False), ("weekend", True))
        },
        "data_quality": {
            "foods": _quality_foods(rows, relevant),
            "research_queue_count_global": queue["queue_count"],
            "research_foods_in_period": len(relevant),
            "research_status_in_period": dict(Counter(f["status"] for f in relevant)),
            "source_gap_occurrences": sum(
                any(n not in r["source_nutrients"] for n in NUTRIENTS) for r in rows
            ),
            "filled_by_scaled_enrichment_occurrences": sum(
                any(n not in r["source_nutrients"] and n in r["estimates"] for n in NUTRIENTS)
                for r in rows
            ),
            "flag_counts": dict(flags),
            "manual_review_foods": [f for f in food_report["catalog"] if f["review_flags"]],
            "complete_macro_calorie_pct": _pct(complete_macro_calories, sum(calories))
            if rows and all(v is not None and v >= 0 for v in calories)
            else None,
            "no_stable_source_id_occurrences": sum(not r["source_food_id"] for r in rows),
        },
        "definitions": {
            "daily_arrays": "Partial known sums; null means no numeric values. Source wins; estimates only fill source gaps. Missing count explicitly accompanies totals.",
            "averages": "Logged-day mean requires full numeric coverage on logged days. Calendar-day intake mean additionally requires every day logged. Recorded contribution/calendar day is not estimated intake on unlogged days.",
            "enrichment": "Numeric use requires reviewed high-confidence reference AND explicit nutrition_basis.occurrence_scaling={method:logged_amount,unit:<exact stored unit>}. Bounds/unscaled references remain research-availability only.",
            "weights": "No interpolation. Rolling observed-day averages require 3 observations/7 calendar days or 7/30, same known unit; may include days before selection.",
            "meals": "Original classification; average per distinct logged date/meal, not per food. Shares null when period nutrient total incomplete.",
            "quality": "Heuristic review flags only, no automatic correction or dietary advice. Research queue remains authoritative for research availability.",
        },
    }
    if compare:
        prior_end = start - timedelta(days=1)
        prior_start = prior_end - timedelta(days=len(days) - 1)
        if period == "month":
            prior_start = prior_end.replace(day=1)
            prior_end = prior_end.replace(
                day=min(end.day, calendar.monthrange(prior_end.year, prior_end.month)[1])
            )
        previous = _summary(
            [
                r
                for r in records
                if prior_start.isoformat() <= r["source_date"] <= prior_end.isoformat()
            ],
            _days(prior_start, prior_end),
        )
        comparisons = {}
        for n in NUTRIENTS:
            current, prior = summary["nutrients"][n], previous["nutrients"][n]
            a, b = current["average_per_calendar_day"], prior["average_per_calendar_day"]
            comparisons[n] = {
                "absolute_change": a - b if a is not None and b is not None else None,
                "percentage_change": 100 * (a - b) / b
                if a is not None and b is not None and b != 0
                else None,
                "coverage_percentage_point_change": current["combined_coverage_pct"]
                - prior["combined_coverage_pct"]
                if current["combined_coverage_pct"] is not None
                and prior["combined_coverage_pct"] is not None
                else None,
            }
        report["comparison"] = {
            "prior_start": prior_start.isoformat(),
            "prior_end": prior_end.isoformat(),
            "metrics": comparisons,
        }
    report["app_summary"] = {
        "period_days": len(days),
        "logging_completeness": report["logging_completeness"],
        "averages": {
            n: summary["nutrients"][n]["average_per_logged_day"]
            for n in ("calories", "protein_g", "fiber_g", "sugar_g", "sodium_mg")
        },
        "protein_adherence": report["consistency"]["protein_g"],
        "weight_change": report["weight_summary"]["change"],
        "insight_slots": [None, None, None],
    }
    return report


def format_analytics(report):
    def display(value):
        return f"{value:.2f}" if value is not None else "unavailable"

    summary = report["logging_completeness"]
    lines = [
        f"Local analytics {report['period']['start']}..{report['period']['end']}",
        f"Logged days: {summary['logged_days']}/{summary['calendar_days']} (missing: {summary['missing_days']})",
    ]
    for n in ("calories", "protein_g"):
        m = report["period_averages"][n]
        lines.append(
            f"{n}: mean/logged day={display(m['average_per_logged_day'])}; numeric coverage={display(m['combined_coverage_pct'])}%"
        )
        if m["average_per_logged_day"] is None and m["complete_logged_days"]:
            lines.append(
                f"  Complete-day subset mean={display(m['average_per_complete_logged_day'])} ({m['complete_logged_days']} days only)"
            )
    lines.append(
        f"Weight change: {display(report['weight_summary']['change'])} {report['weight_summary']['unit'] or '(unit unavailable)'}"
    )
    foods = report["food_contributors"]
    for n in ("calories", "protein_g"):
        lines.append(
            f"Top {n} foods: "
            + ", ".join(
                " ".join(foods["catalog"][i]["food_name"].split()) for i in foods["rankings"][n][:3]
            )
        )
    lines.append(
        f"Research queue: {report['data_quality']['research_queue_count_global']} foods globally; review flags: {report['data_quality']['flag_counts']}"
    )
    gaps = [
        f"{n} ({m['missing_count']})"
        for n, m in report["nutrient_coverage"].items()
        if m["missing_count"]
    ]
    lines.append("Missing nutrient occurrences: " + (", ".join(gaps) or "none"))
    if "comparison" in report:
        lines.append(
            "Prior-period calorie mean change: "
            + display(report["comparison"]["metrics"]["calories"]["absolute_change"])
        )
    lines.append(
        "null/None means unavailable or incomplete, never zero. Use --json for coverage and comparisons."
    )
    return "\n".join(lines)
