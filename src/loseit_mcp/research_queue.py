"""Compact local research worklist and conservative source-ID import guards."""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from .repository import (
    PROVENANCE_TYPES,
    SCHEMA_VERSION,
    STANDARD_NUTRIENTS,
    current_filter,
    normalize_text,
)

EXACT_MATCHES = {"exact_brand_product", "USDA_or_other_authoritative_generic"}


def _finite(value: Any) -> bool:
    return not isinstance(value, bool) and isinstance(value, int | float) and math.isfinite(value)


def _json(value: Any) -> str:
    return json.dumps(
        value, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":")
    )


def _hash(value: Any) -> str:
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _table(connection: sqlite3.Connection, name: str) -> bool:
    return (
        connection.execute("SELECT 1 FROM sqlite_master WHERE name=?", (name,)).fetchone()
        is not None
    )


def _groups(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    observations: dict[int, dict[str, float]] = defaultdict(dict)
    for row in connection.execute(
        "SELECT occurrence_id,nutrient,value FROM nutrient_observations WHERE provenance='loseit'"
    ):
        if row["nutrient"] in STANDARD_NUTRIENTS and _finite(row["value"]):
            observations[row["occurrence_id"]][row["nutrient"]] = row["value"]
    groups: dict[tuple, dict] = {}
    snapshots: dict[int, dict] = {}
    for row in connection.execute("SELECT * FROM food_occurrences" + current_filter(connection) + " ORDER BY id"):
        key = (
            (row["source"], "id", row["source_food_id"])
            if row["source_food_id"]
            else (row["source"], "name", row["food_name_normalized"], row["brand_normalized"])
        )
        group = groups.setdefault(
            key,
            {
                "rows": [],
                "variants": {},
                "source": row["source"],
                "source_food_id": row["source_food_id"] or None,
            },
        )
        description = None
        if row["source_entry_id"]:
            sid = row["raw_snapshot_id"]
            if sid not in snapshots:
                raw = connection.execute(
                    "SELECT payload_json FROM raw_diary_snapshots WHERE id=?", (sid,)
                ).fetchone()
                snapshots[sid] = json.loads(raw[0]) if raw else {}
            entries = [
                e
                for e in snapshots[sid].get("entries", [])
                if str(e.get("entry_id")) == row["source_entry_id"]
            ]
            if len(entries) == 1 and isinstance(entries[0].get("serving_description"), str):
                description = entries[0]["serving_description"]
        context = {
            "food_name_raw": row["food_name_raw"],
            "brand_raw": row["brand_raw"],
            "food_name_normalized": row["food_name_normalized"],
            "brand_normalized": row["brand_normalized"],
            "logged_portion": {
                "amount": row["amount"] if _finite(row["amount"]) else None,
                "unit": row["unit"],
                "servings": row["servings"] if _finite(row["servings"]) else None,
                "description": description,
            },
            "source_nutrients": observations[row["id"]],
        }
        signature = _hash(context)
        variant = group["variants"].setdefault(signature, context | {"occurrence_count": 0})
        variant["occurrence_count"] += 1
        group["rows"].append(dict(row) | {"source_nutrients": observations[row["id"]]})
    for group in groups.values():
        group["variants"] = sorted(group["variants"].values(), key=_json)
        group["context"] = [
            {k: v for k, v in variant.items() if k != "occurrence_count"}
            for variant in group["variants"]
        ]
        group["context"] = sorted(group["context"], key=_json)
    return list(groups.values())


def _conflicts(contexts: list[dict]) -> bool:
    identities = {(c["food_name_normalized"], c["brand_normalized"]) for c in contexts}
    if len(identities) > 1:
        return True
    # Only compare identical stored portions. Do not infer a per-serving conversion.
    for i, left in enumerate(contexts):
        for right in contexts[i + 1 :]:
            if left["logged_portion"] == right["logged_portion"]:
                shared = left["source_nutrients"].keys() & right["source_nutrients"].keys()
                if any(left["source_nutrients"][n] != right["source_nutrients"][n] for n in shared):
                    return True
    return False


def validate_import_target(
    connection: sqlite3.Connection, document: dict
) -> tuple[str | None, list]:
    """Validate a queue target against the current source snapshot before any import writes."""
    target = document.get("target")
    if target is None:
        if "source_food_id" in document or "import_target" in document:
            raise ValueError(
                "Copy the queue import_target object into the enrichment 'target' field"
            )
        return None, []  # Existing name/brand import contract is retained.
    if not isinstance(target, dict) or target.get("source") != "loseit":
        raise ValueError("target must identify a Lose It source food")
    food_id = target.get("source_food_id")
    if not isinstance(food_id, str) or not food_id.strip():
        raise ValueError(
            "Stable target.source_food_id is required; use reviewed name/brand import for no-ID foods"
        )
    groups = [
        g for g in _groups(connection) if g["source"] == "loseit" and g["source_food_id"] == food_id
    ]
    if len(groups) != 1:
        raise ValueError("Target source food ID does not exist in this repository")
    group = groups[0]
    if target.get("source_context_sha256") != _hash(group["context"]):
        raise ValueError(
            "Source context changed; regenerate research queue and review before importing"
        )
    identity = {(r["food_name_normalized"], r["brand_normalized"]) for r in group["rows"]}
    requested = (
        normalize_text(str(document.get("food_name") or "")),
        normalize_text(str(document.get("brand") or "")),
    )
    if identity != {requested} or _conflicts(group["context"]):
        raise ValueError(
            "Conflicting source identity or values require explicit review before import"
        )
    if (target.get("food_name_normalized"), target.get("brand_normalized")) != requested:
        raise ValueError("Target name/brand do not match the reviewed source identity")
    if not isinstance(document.get("manually_reviewed"), bool):
        raise TypeError("manually_reviewed must be a JSON boolean")
    basis = document.get("nutrition_basis")
    if not isinstance(basis, dict) or not str(basis.get("description") or "").strip():
        raise ValueError(
            "ID-targeted enrichment requires a nutrition_basis description from the reviewed label"
        )
    if (
        not _finite(basis.get("amount"))
        or basis["amount"] <= 0
        or not isinstance(basis.get("unit"), str)
        or not basis["unit"].strip()
    ):
        raise ValueError("nutrition_basis requires a positive finite amount and an explicit unit")
    for item in document.get("nutrients") or []:
        if not isinstance(item, dict):
            raise TypeError("nutrients must contain records")
        point = item.get("estimated_value")
        lower, upper = item.get("lower_bound"), item.get("upper_bound")
        if point is not None and not _finite(point):
            raise ValueError("Estimated nutrient values must be finite")
        if (lower is not None or upper is not None) and (
            not _finite(lower) or not _finite(upper) or lower > upper
        ):
            raise ValueError("Nutrient bounds must be finite and ordered")
        if point is None and lower is None:
            raise ValueError("Each nutrient requires a value or finite bounds")
    return food_id, group["context"]


def read_research_queue(data_dir: Path) -> dict[str, Any]:
    path = (data_dir.expanduser() / "nutrition.sqlite3").resolve()
    c = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    try:
        c.execute("PRAGMA query_only=ON")
        c.execute("BEGIN")
        version = c.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
        if version not in range(1, SCHEMA_VERSION + 1):
            raise ValueError("Unsupported repository schema")
        return _queue(c)
    finally:
        c.close()


def _queue(c: sqlite3.Connection) -> dict[str, Any]:
    links = {
        r["occurrence_id"]: dict(r)
        for r in c.execute(
            "SELECT l.occurrence_id,l.match_method,v.* FROM occurrence_reference_links l "
            "JOIN latest_enrichment_versions v ON v.reference_id=l.reference_id"
        )
    }
    estimates: dict[int, list[dict]] = defaultdict(list)
    for r in c.execute("SELECT * FROM estimated_nutrients"):
        if r["nutrient"] in STANDARD_NUTRIENTS:
            estimates[r["enrichment_version_id"]].append(dict(r))
    review_contexts = (
        {
            r[0]: json.loads(r[1])
            for r in c.execute(
                "SELECT enrichment_version_id,source_context_json FROM enrichment_review_contexts"
            )
        }
        if _table(c, "enrichment_review_contexts")
        else {}
    )
    general_queue = [
        dict(r)
        for r in c.execute(
            "SELECT food_name_normalized,brand_normalized,source_food_id,status FROM enrichment_queue"
        )
    ]
    targets = (
        [dict(r) for r in c.execute("SELECT * FROM source_reference_targets")]
        if _table(c, "source_reference_targets")
        else []
    )
    records = []
    lifecycle: Counter = Counter()
    for group in _groups(c):
        rows, context = group["rows"], group["context"]
        latest = rows[-1]
        missing_counts: Counter = Counter()
        source_missing: set[str] = set()
        filled: set[str] = set()
        reference_states = {}
        conflict = _conflicts(context) or any(
            t["source"] == group["source"]
            and t["source_food_id"] == group["source_food_id"]
            and any(
                (r["food_name_normalized"], r["brand_normalized"])
                != (t["food_name_normalized"], t["brand_normalized"])
                for r in rows
            )
            for t in targets
        )
        needs_review = conflict
        for row in rows:
            present = set(row["source_nutrients"])
            gaps = set(STANDARD_NUTRIENTS) - present
            source_missing |= gaps
            link = links.get(row["id"])
            usable = set()
            if link:
                past = review_contexts.get(link["id"], [])
                changed = bool(past) and _conflicts(past + context)
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
                    and not conflict
                    and not changed
                )
                reference_states[link["id"]] = {
                    "reference_id": link["reference_id"],
                    "version": link["version"],
                    "match_method": link["match_method"],
                    "match_type": link["match_type"],
                    "confidence": link["confidence"],
                    "manually_reviewed": bool(link["manually_reviewed"]),
                    "eligible_for_reuse": trusted,
                    "source_context_conflict": changed or conflict,
                }
                if not trusted and gaps:
                    needs_review = True
                if trusted:
                    for item in estimates[link["id"]]:
                        bounds = (
                            _finite(item["lower_bound"])
                            and _finite(item["upper_bound"])
                            and item["lower_bound"] <= item["upper_bound"]
                        )
                        if item["provenance"] in PROVENANCE_TYPES and (
                            _finite(item["estimated_value"]) or bounds
                        ):
                            usable.add(item["nutrient"])
                filled |= gaps & usable
            missing_counts.update(gaps - usable)
        missing = [n for n in STANDARD_NUTRIENTS if missing_counts[n]]
        status = (
            "complete"
            if not missing
            else (
                "needs_review"
                if needs_review
                else "partially_filled"
                if filled
                else "needs_research"
            )
        )
        lifecycle[status] += 1
        if not missing:
            continue
        identical = len({_json(r["source_nutrients"]) for r in rows}) == 1
        name, brand = latest["food_name_normalized"], latest["brand_normalized"]
        target = (
            {
                "source": group["source"],
                "source_food_id": group["source_food_id"],
                "food_name_normalized": name,
                "brand_normalized": brand,
                "source_context_sha256": _hash(context),
            }
            if group["source_food_id"]
            else None
        )
        records.append(
            {
                "source_food_id": group["source_food_id"],
                "food_name": name,
                "brand": brand,
                "food_name_raw": latest["food_name_raw"],
                "brand_raw": latest["brand_raw"],
                "occurrence_count": len(rows),
                "first_seen": min(r["source_date"] for r in rows),
                "last_seen": max(r["source_date"] for r in rows),
                "status": status,
                "reference_status": list(reference_states.values()),
                "general_cache_statuses": sorted(
                    {
                        q["status"]
                        for q in general_queue
                        if (
                            (
                                group["source_food_id"]
                                and q["source_food_id"] == group["source_food_id"]
                            )
                            or (
                                not group["source_food_id"]
                                and not q["source_food_id"]
                                and q["food_name_normalized"] == name
                                and q["brand_normalized"] == brand
                            )
                        )
                    }
                ),
                "source_serving": None,
                "source_basis_status": "Original label basis not retained; stored occurrence values and portions follow. Do not infer a per-serving label.",
                "source_nutrients": rows[0]["source_nutrients"] if identical else None,
                "source_values_identical": identical,
                "portion_variants": group["variants"],
                "source_missing_standard_nutrients": [
                    n for n in STANDARD_NUTRIENTS if n in source_missing
                ],
                "missing_standard_nutrients": missing,
                "missing_occurrence_counts": {n: missing_counts[n] for n in missing},
                "priority_score": len(rows) * len(missing),
                "matching_status": "ambiguous_source_identity_or_values"
                if conflict
                else "brand_supplied_product_match_unverified"
                if brand
                else "unbranded_match_unverified",
                "import_target": target,
            }
        )
    records.sort(
        key=lambda r: (
            -r["priority_score"],
            -r["occurrence_count"],
            r["food_name"],
            r["brand"],
            r["source_food_id"] or "",
        )
    )
    return {
        "schema_version": 1,
        "local_only": True,
        "queue_count": len(records),
        "standard_nutrients": list(STANDARD_NUTRIENTS),
        "lifecycle_counts": dict(sorted(lifecycle.items())),
        "rules": {
            "priority": "occurrence_count × number of missing standard nutrients; heuristic, not scientific",
            "reuse": "Latest linked version only: reviewed, high confidence, exact branded/authoritative generic match or manually confirmed alias. Other enrichment needs review.",
            "values": "Finite source values exactly as stored, including zero. Unknown nutrient keys excluded. No scaling, estimates or product identities inferred.",
            "combined": "Source availability plus eligible linked estimates/bounds; not a guarantee of portion-normalized nutrient totals.",
            "status": "Derived each run: needs_research, needs_review, partially_filled, complete. Complete foods omitted; version history retained.",
            "import": "One enrichment object per file. Copy import_target to target; retain food_name and brand. Add reviewed nutrition_basis and normal provenance/version fields. No-ID foods require conservative name/brand import.",
        },
        "foods": records,
    }


def format_research_queue(report: dict) -> str:
    lines = [
        f"Standard nutrient research queue: {report['queue_count']} foods (local only)",
        "Priority = occurrence frequency × missing standard fields; unknown fields excluded.",
    ]
    for row in report["foods"]:
        name = " ".join(row["food_name"].split())
        brand = " ".join(row["brand"].split())
        lines.append(
            f"{name} ({brand or 'no brand'}) | {row['occurrence_count']} occurrences | "
            f"score={row['priority_score']} | {row['status']}"
        )
        lines.append("  Missing: " + ", ".join(row["missing_standard_nutrients"]))
    lines.append(
        "Use --json for actual source values, distinct portions, matching context, and import targets."
    )
    return "\n".join(lines)
