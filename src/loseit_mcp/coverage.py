"""Local nutrition availability reporting; no authentication or network dependencies."""

from __future__ import annotations

import math
import sqlite3
from collections import defaultdict
from pathlib import Path
from textwrap import fill
from typing import Any

from .repository import PROVENANCE_TYPES, SCHEMA_VERSION, STANDARD_NUTRIENTS


def _finite(value: Any) -> bool:
    return isinstance(value, int | float) and math.isfinite(value)


def read_coverage(data_dir: Path, *, missing_only: bool = False) -> dict[str, Any]:
    """Open an existing database read-only, without migrations or directory creation."""
    path = (data_dir.expanduser() / "nutrition.sqlite3").resolve()
    connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")  # One consistent snapshot, including committed WAL data.
        version = connection.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
        if version not in range(1, SCHEMA_VERSION + 1):
            raise ValueError(f"Coverage requires database schema {SCHEMA_VERSION}; found {version}")
        return _report(connection, missing_only=missing_only)
    finally:
        connection.close()


def _report(connection: sqlite3.Connection, *, missing_only: bool) -> dict[str, Any]:
    rows = connection.execute(
        "SELECT id,source,source_food_id,food_name_normalized,brand_normalized "
        "FROM food_occurrences ORDER BY id"
    ).fetchall()
    ids = {row["id"] for row in rows}
    source: dict[str, set[int]] = defaultdict(set)
    estimated: dict[str, set[int]] = defaultdict(set)
    bounded: dict[str, set[int]] = defaultdict(set)
    calories: dict[int, float] = {}
    units: dict[str, set[str]] = defaultdict(set)
    targets = set(STANDARD_NUTRIENTS)
    for row in connection.execute("SELECT * FROM nutrient_observations"):
        nutrient, oid = row["nutrient"], row["occurrence_id"]
        targets.add(nutrient)
        units[nutrient].add(row["unit"])
        if oid in ids and row["provenance"] == "loseit" and _finite(row["value"]):
            source[nutrient].add(oid)
            if nutrient == "calories":
                calories[oid] = row["value"]
    # Discover every stored field, but only linked *latest-version* values count.
    for row in connection.execute("SELECT DISTINCT nutrient,unit FROM estimated_nutrients"):
        targets.add(row["nutrient"])
        units[row["nutrient"]].add(row["unit"])
    links = {
        row["occurrence_id"]: dict(row)
        for row in connection.execute(
            "SELECT l.occurrence_id,l.match_method,l.confidence AS link_confidence,"
            "v.confidence,v.manually_reviewed FROM occurrence_reference_links l "
            "LEFT JOIN latest_enrichment_versions v ON v.reference_id=l.reference_id"
        )
    }
    for row in connection.execute(
        "SELECT l.occurrence_id,n.* FROM occurrence_reference_links l "
        "JOIN latest_enrichment_versions v ON v.reference_id=l.reference_id "
        "JOIN estimated_nutrients n ON n.enrichment_version_id=v.id"
    ):
        oid, nutrient = row["occurrence_id"], row["nutrient"]
        if oid not in ids or row["provenance"] not in PROVENANCE_TYPES:
            continue
        point = _finite(row["estimated_value"])
        interval = (
            _finite(row["lower_bound"])
            and _finite(row["upper_bound"])
            and row["lower_bound"] <= row["upper_bound"]
        )
        if point or interval:
            estimated[nutrient].add(oid)
            if not point:
                bounded[nutrient].add(oid)
    queues = connection.execute(
        "SELECT food_name_normalized,brand_normalized,source_food_id,status FROM enrichment_queue"
    ).fetchall()
    queue_status: dict[tuple[str, ...], set[str]] = defaultdict(set)
    for row in queues:
        key = _food_key(
            "loseit", row["source_food_id"], row["food_name_normalized"], row["brand_normalized"]
        )
        queue_status[key].add(row["status"])
    nutrients = list(STANDARD_NUTRIENTS) + sorted(targets - set(STANDARD_NUTRIENTS))

    def coverage(subset: set[int], nutrient: str) -> dict[str, Any]:
        reported = subset & source[nutrient]
        enriched = subset & estimated[nutrient]
        combined = reported | enriched
        total = len(subset)
        valid_calories = (
            bool(subset) and subset <= calories.keys() and all(calories[i] >= 0 for i in subset)
        )
        denominator = sum(calories[i] for i in subset) if valid_calories else 0

        def measure(present: set[int]) -> dict[str, Any]:
            return {
                "occurrences_with_value": len(present),
                "total_occurrences": total,
                "percentage": round(100 * len(present) / total, 2) if total else None,
                "missing_occurrences": total - len(present),
                "calorie_weighted_percentage": (
                    round(100 * sum(calories[i] for i in present) / denominator, 2)
                    if denominator > 0
                    else None
                ),
            }

        return {
            "units": sorted(units[nutrient]),
            "source_reported": measure(reported),
            "estimated_or_enriched": measure(enriched)
            | {
                "bounds_only_occurrences": len(subset & bounded[nutrient]),
                "fills_source_missing_occurrences": len(enriched - reported),
            },
            "combined_usable": measure(combined),
            "calorie_weighting": (
                "source_calories"
                if denominator > 0
                else "unavailable: requires source calories for every occurrence, all nonnegative, "
                "with a positive total"
            ),
        }

    groups: dict[tuple[str, ...], list[sqlite3.Row]] = defaultdict(list)
    for row in rows:
        groups[
            _food_key(
                row["source"],
                row["source_food_id"],
                row["food_name_normalized"],
                row["brand_normalized"],
            )
        ].append(row)
    foods = []
    for key, members in groups.items():
        subset = {row["id"] for row in members}
        details = {nutrient: coverage(subset, nutrient) for nutrient in nutrients}
        missing = [n for n, c in details.items() if c["combined_usable"]["missing_occurrences"]]
        source_missing = [
            n for n, c in details.items() if c["source_reported"]["missing_occurrences"]
        ]
        slots = sum(c["combined_usable"]["missing_occurrences"] for c in details.values())
        latest = members[-1]
        linked = [links[i] for i in subset if i in links]
        foods.append(
            {
                "food_name_normalized": latest["food_name_normalized"],
                "brand_normalized": latest["brand_normalized"],
                "source_food_id": latest["source_food_id"] or None,
                "grouping": "source_food_id"
                if latest["source_food_id"]
                else "normalized_name_brand",
                "occurrence_count": len(subset),
                "nutrients": details,
                "missing_nutrients": missing,
                "missing_nutrient_count": len(missing),
                "source_missing_nutrients": source_missing,
                "source_missing_nutrient_count": len(source_missing),
                "standard_source_complete": not set(source_missing).intersection(
                    STANDARD_NUTRIENTS
                ),
                "priority_score": slots,
                "average_missing_nutrients_per_occurrence": round(slots / len(subset), 2),
                "reference_status": {
                    "linked_occurrences": len(linked),
                    "unlinked_occurrences": len(subset) - len(linked),
                    "queue_statuses": sorted(queue_status[key]),
                    "match_methods": sorted({r["match_method"] for r in linked}),
                    "enrichment_confidences": sorted(
                        {r["confidence"] for r in linked if r["confidence"]}
                    ),
                    "reviewed_occurrences": sum(bool(r["manually_reviewed"]) for r in linked),
                },
            }
        )
    foods.sort(
        key=lambda f: (
            -f["priority_score"],
            -f["occurrence_count"],
            f["food_name_normalized"],
            f["brand_normalized"],
            f["source_food_id"] or "",
        )
    )
    return {
        "report_version": 1,
        "local_only": True,
        "total_occurrences": len(ids),
        "total_foods": len(foods),
        "foods_with_missing_nutrients": sum(bool(f["missing_nutrient_count"]) for f in foods),
        "foods_with_complete_standard_source": sum(f["standard_source_complete"] for f in foods),
        "unresolved_queue_count": sum(r["status"] == "unresolved" for r in queues),
        "unresolved_foods_with_complete_source": sum(
            "unresolved" in f["reference_status"]["queue_statuses"]
            and f["source_missing_nutrient_count"] == 0
            for f in foods
        ),
        "target_nutrients": nutrients,
        "definitions": {
            "targets": "Nine standard nutrients plus every nutrient field stored in the database.",
            "unmapped_fields": "unknown_nutrient_* keys and source_unit labels are preserved as "
            "stored. Their identities and units are not inferred. They participate in full-target "
            "coverage and priority; standard_source_complete covers only the nine named nutrients.",
            "source_reported": "Finite source observation; explicit zero counts, absent value does not.",
            "estimated_or_enriched": "Existing occurrence link to latest reference version with a "
            "finite estimate or finite ordered bounds; includes low confidence and unreviewed values.",
            "combined_usable": "Union of source and linked estimate availability, counted once. "
            "Availability does not certify accuracy or portion scaling; no nutrient totals are inferred.",
            "priority_score": "Occurrence count × average combined missing nutrients per occurrence "
            "= missing occurrence-nutrient slots. Practical sorting heuristic, not a scientific score.",
            "unresolved": "No cached reference; independent of missing nutrition. Complete source "
            "nutrition can coexist with unresolved enrichment status.",
            "missing_only": "Filters foods with combined missing target nutrients; overall totals unchanged.",
        },
        "nutrients": {nutrient: coverage(ids, nutrient) for nutrient in nutrients},
        "missing_only": missing_only,
        "foods": [f for f in foods if not missing_only or f["missing_nutrient_count"]],
    }


def _food_key(source: str, food_id: str | None, name: str, brand: str) -> tuple[str, ...]:
    return (source, "id", food_id) if food_id else (source, "name_brand", name, brand)


def format_coverage(report: dict[str, Any]) -> str:
    """Concise terminal overview; JSON carries every per-food coverage measure."""
    lines = [
        (
            f"Nutrition coverage: {report['total_occurrences']} occurrences, "
            f"{report['total_foods']} foods (local database)"
        ),
        (
            f"Unresolved references: {report['unresolved_queue_count']}; "
            f"{report['unresolved_foods_with_complete_source']} unresolved foods have complete source coverage across all targets."
        ),
        f"Complete source coverage of the nine standard nutrients: {report['foods_with_complete_standard_source']} foods.",
        "Unresolved reference status does not mean missing nutrition.",
        "Unmapped unknown_nutrient_* fields are preserved; no identities or units are inferred.",
        "",
        f"{'Nutrient':<24} {'Source':<17} {'Estimated':<17} {'Combined':<17} Missing  Calorie-weighted S/E/C",
    ]
    for nutrient, item in report["nutrients"].items():
        measures = [
            item[k] for k in ("source_reported", "estimated_or_enriched", "combined_usable")
        ]
        counts = [
            f"{m['occurrences_with_value']}/{m['total_occurrences']} ({m['percentage']}%)"
            for m in measures
        ]
        weighted = "/".join(
            "n/a"
            if m["calorie_weighted_percentage"] is None
            else f"{m['calorie_weighted_percentage']}%"
            for m in measures
        )
        lines.append(
            f"{nutrient:<24} {counts[0]:<17} {counts[1]:<17} {counts[2]:<17} "
            f"{measures[2]['missing_occurrences']:>4}  {weighted}"
        )
    lines += [
        "",
        "Foods, highest practical impact first (frequency × average combined gaps):",
        "Missing lists mean at least one occurrence lacks that nutrient; partial source counts shown.",
    ]
    for food in report["foods"]:
        name = " ".join(food["food_name_normalized"].split())
        brand = " ".join(food["brand_normalized"].split())
        status = ",".join(food["reference_status"]["queue_statuses"]) or "not queued"
        lines.append(
            f"{name} ({brand or 'no brand'}) | id={food['source_food_id'] or 'unavailable'} | "
            f"n={food['occurrence_count']} score={food['priority_score']} | "
            f"references={food['reference_status']['linked_occurrences']} linked; {status}"
        )
        present = [
            f"{n}:{c['source_reported']['occurrences_with_value']}/{food['occurrence_count']}"
            for n, c in food["nutrients"].items()
        ]
        lines.append(
            fill(
                "Source present: " + ", ".join(present),
                width=110,
                initial_indent="  ",
                subsequent_indent="    ",
            )
        )
        lines.append(
            fill(
                f"Combined missing ({food['missing_nutrient_count']}): "
                + (", ".join(food["missing_nutrients"]) or "none"),
                width=110,
                initial_indent="  ",
                subsequent_indent="    ",
            )
        )
    lines += [
        "",
        (
            "Zero is a reported value; absent values remain missing. Estimates are linked reference "
            "availability, including bounds; they do not certify accuracy or portion scaling."
        ),
        "Calorie weighting requires complete nonnegative source calories and a positive total.",
    ]
    return "\n".join(lines)
