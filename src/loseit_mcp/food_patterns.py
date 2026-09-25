"""Conservative, versioned food-pattern evidence over the Resolved Food Library.

This module never contacts Lose It. Automatic rules deliberately recognize only
unambiguous standalone foods; mixed dishes retain directional/unknown amounts.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

from .repository import NutritionRepository, _now, normalize_text
from .resolved import serving_scale

PATTERNS = (
    "vegetable", "fruit", "seafood", "fatty_fish", "whole_grain",
    "legume", "nut_seed", "plant_variety",
)
PATTERN_REFERENCES = {
    "vegetable": "MyPlate example: about 2.5 cups/day at 2,000 kcal; not personalized",
    "fruit": "MyPlate example: about 2 cups/day at 2,000 kcal; not personalized",
    "seafood": "AHA: about two fish servings/week; quantity here unverified",
    "fatty_fish": "AHA emphasizes fatty fish within seafood choices",
    "whole_grain": "Guidance favors whole grains; quantity here unverified",
    "legume": "Dietary variety; no standalone target applied",
    "nut_seed": "Dietary variety; no standalone target applied",
    "plant_variety": "Exploratory distinct-plant count; no clinical target",
}
QUALIFYING = {"meaningful", "substantial"}
GENERIC_ZERO_IDENTITIES = {
    "Milk, 2%, w/ Vitamin A & D": "Plain two-percent milk; naturally occurring lactose is not added sugar",
    "Rice, White, Cooked": "Plain cooked white rice identity",
    "Broccoli Florets Frozen": "Plain frozen broccoli identity",
    "Eggs, Large": "Plain whole egg identity",
}
GENERIC_ZERO_REFERENCE = "https://aglab.ars.usda.gov/projects-nutrition-corner/take-healthy-eating-challenge"
CURRENT_LABEL_ZEROS = {
    ("Broccoli Florets Frozen", "Great Value"): (
        1.0, "cup", "https://www.walmart.com/ip/735585383",
        "Current exact Great Value frozen broccoli listing states no added sugars; historical formulation unverified",
    ),
    ("Protein Powder, Cocoa Pebbles", "Dymatize"): (
        32.0, "g", "https://dymatize.com/products/iso100-cocoa-pebbles",
        "Current ISO100 Cocoa Pebbles label: 0 g added sugar per 32 g scoop; logged gram/calorie basis needs reconciliation",
    ),
}


def build_usda_pattern_provider():
    """Return a bounded exact-identity USDA reviewer when a safe key exists."""
    from .credentials import CredentialError, load_usda_api_key
    from .nutrition_provider import UsdaFoodDataCentralWorker

    try:
        key = load_usda_api_key()
    except CredentialError:
        return None
    if not key:
        return None
    worker = UsdaFoodDataCentralWorker(key, timeout=8)

    def research(candidate: dict) -> list[dict]:
        name = normalize_text(candidate["name"])
        brand = normalize_text(candidate.get("brand", ""))
        # Mixed dishes, branded recipes and garnish-sized ingredients require
        # manual review; a USDA category cannot quantify their components.
        excluded = {"pizza", "soup", "sauce", "taco", "sandwich", "burger",
                    "seasoned", "sweetened", "fried", "breaded", "juice", "shake"}
        if brand or len(name.split()) > 5 or any(word in name.split() for word in excluded):
            return []
        payload = worker._request(
            "POST", "/foods/search",
            json={"query": candidate["name"],
                  "dataType": ["Foundation", "SR Legacy"], "pageSize": 10},
        )
        exact = [food for food in payload.get("foods", []) if
                 normalize_text(str(food.get("description", ""))) == name and
                 str(food.get("dataType", "")).casefold() != "branded"]
        if len(exact) != 1:
            return []
        food = exact[0]
        category = normalize_text(str(food.get("foodCategory", "")))
        patterns = []
        if "vegetable" in category:
            patterns.append("vegetable")
        elif "fruit" in category:
            patterns.append("fruit")
        elif "legume" in category:
            patterns.append("legume")
        elif "nut and seed" in category:
            patterns.append("nut_seed")
        elif "finfish" in category or "shellfish" in category:
            patterns.append("seafood")
            if any(word in name.split() for word in ("salmon", "sardines", "trout", "mackerel")):
                patterns.append("fatty_fish")
        if not patterns:
            return []
        reference = f"https://fdc.nal.usda.gov/food-details/{food['fdcId']}/nutrients"
        identity = name.split()[0]
        return [{"pattern_key": pattern,
                 "presence_class": "meaningful",
                 "plant_identity": identity if pattern in
                 {"vegetable", "fruit", "legume", "nut_seed"} else None,
                 "provenance": "USDA_exact_generic_category",
                 "confidence": "high",
                 "evidence_basis": f"Exact USDA generic identity; category: {category}",
                 "evidence_url": reference} for pattern in patterns]

    return research


def _digest(value: dict) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _obvious(name: str) -> list[tuple[str, str, str | None, str]]:
    """Return (category, presence, plant identity, basis) for narrow identities."""
    n = " ".join(name.casefold().replace(",", " ").split())
    if n in {"broccoli fresh", "broccoli florets frozen"}:
        return [("vegetable", "meaningful", "broccoli", "Standalone broccoli identity")]
    if n in {"fish salmon new orleans", "fish pink salmon cooked"}:
        return [
            ("seafood", "meaningful", None, "Standalone salmon identity"),
            ("fatty_fish", "meaningful", None, "Salmon is fatty fish"),
        ]
    if n == "blackened shrimp tacos":
        return [("seafood", "presence_known_quantity_unknown", None,
                 "Shrimp named in mixed dish; shrimp amount unknown")]
    if n in {"beans pinto", "kroger brown sugar hickory baked brans"}:
        identity = "pinto bean" if n == "beans pinto" else "bean unspecified"
        return [("legume", "meaningful", identity, "Named bean food; quantity unverified")]
    if n in {"100% whole wheat bagel", "pasta whole wheat elbows (dry)"}:
        return [("whole_grain", "meaningful", "wheat", "Explicit whole-wheat identity")]
    if n in {"vegetables fajita", "corn salsa"}:
        return [("vegetable", "presence_known_quantity_unknown", None,
                 "Vegetable named, exact component amount unknown")]
    if n in {"banana raw", "banana", "apple raw", "apple fresh", "orange raw", "blueberries raw"}:
        identity = n.split()[0]
        return [("fruit", "meaningful", identity, "Standalone whole fruit identity")]
    if n in {"almonds", "almonds raw", "walnuts", "walnuts raw", "peanuts", "peanuts raw"}:
        return [("nut_seed", "meaningful", n.split()[0].rstrip("s"),
                 "Standalone nut identity")]
    return []


def record_pattern_evidence(
    c: sqlite3.Connection, *, canonical_food_id: int, formulation_id: int | None,
    pattern_key: str, presence_class: str, provenance: str, confidence: str,
    evidence_basis: str, plant_identity: str | None = None,
    vegetable_subgroup: str | None = None,
    quantity_kind: str = "unquantified", equivalent_value: float | None = None,
    equivalent_unit: str | None = None, lower_bound: float | None = None,
    upper_bound: float | None = None, evidence_url: str | None = None,
    valid_from: str | None = None, valid_to: str | None = None,
    user_confirmed: bool = False, supersedes_id: int | None = None,
) -> bool:
    """Append one reviewed assertion, or reuse an identical assertion."""
    if pattern_key not in set(PATTERNS) - {"plant_variety"}:
        raise ValueError("Unsupported food-pattern category")
    if not evidence_basis.strip() or not provenance.strip():
        raise ValueError("Evidence and provenance are required")
    if valid_from and valid_to and valid_from > valid_to:
        raise ValueError("Invalid pattern evidence date range")
    if formulation_id is not None:
        owner = c.execute(
            "SELECT canonical_food_id FROM food_formulations WHERE id=?", (formulation_id,)
        ).fetchone()
        if not owner or owner[0] != canonical_food_id:
            raise ValueError("Formulation does not belong to canonical food")
    if supersedes_id is not None:
        prior = c.execute(
            "SELECT canonical_food_id,formulation_id,pattern_key FROM food_pattern_evidence WHERE id=?",
            (supersedes_id,),
        ).fetchone()
        if not prior or tuple(prior) != (canonical_food_id, formulation_id, pattern_key):
            raise ValueError("Superseded pattern evidence must describe the same formulation/category")
    payload = {
        "canonical_food_id": canonical_food_id, "formulation_id": formulation_id,
        "pattern_key": pattern_key, "presence_class": presence_class, "provenance": provenance,
        "confidence": confidence, "evidence_basis": evidence_basis,
        "plant_identity": plant_identity, "quantity_kind": quantity_kind,
        "vegetable_subgroup": vegetable_subgroup,
        "equivalent_value": equivalent_value, "equivalent_unit": equivalent_unit,
        "lower_bound": lower_bound, "upper_bound": upper_bound, "evidence_url": evidence_url,
        "valid_from": valid_from, "valid_to": valid_to,
        "user_confirmed": int(user_confirmed), "supersedes_id": supersedes_id,
    }
    result = c.execute(
        """INSERT OR IGNORE INTO food_pattern_evidence
           (canonical_food_id,formulation_id,pattern_key,plant_identity,vegetable_subgroup,presence_class,
            quantity_kind,equivalent_value,equivalent_unit,lower_bound,upper_bound,
            provenance,confidence,evidence_basis,evidence_url,valid_from,valid_to,
            user_confirmed,supersedes_id,content_sha256,created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (canonical_food_id, formulation_id, pattern_key, plant_identity, vegetable_subgroup, presence_class,
         quantity_kind, equivalent_value, equivalent_unit, lower_bound, upper_bound,
         provenance, confidence, evidence_basis, evidence_url, valid_from, valid_to,
         int(user_confirmed), supersedes_id, _digest(payload), _now()),
    )
    return result.rowcount == 1


def classify_obvious(c: sqlite3.Connection) -> dict:
    """Idempotently record narrow name-based evidence for latest formulations."""
    created = 0
    examined = 0
    for row in c.execute(
        """SELECT f.id,f.canonical_name,v.id FROM canonical_foods f
           JOIN food_formulations v ON v.canonical_food_id=f.id
           WHERE v.version=(SELECT MAX(version) FROM food_formulations WHERE canonical_food_id=f.id)
           ORDER BY f.id"""
    ).fetchall():
        examined += 1
        for key, presence, plant, basis in _obvious(row[1]):
            created += record_pattern_evidence(
                c, canonical_food_id=row[0], formulation_id=row[2], pattern_key=key,
                presence_class=presence, plant_identity=plant,
                vegetable_subgroup="dark_green" if plant == "broccoli" else None,
                provenance="deterministic_identity_rule_v1", confidence="medium",
                evidence_basis=basis,
            )
    return {"foods_examined": examined, "classifications_created": created}


def record_added_sugar(
    c: sqlite3.Connection, *, canonical_food_id: int, formulation_id: int,
    value_g: float, basis_amount: float, basis_unit: str, provenance: str,
    confidence: str, evidence_basis: str, evidence_url: str | None = None,
    valid_from: str | None = None, valid_to: str | None = None,
    user_confirmed: bool = False, supersedes_id: int | None = None,
) -> bool:
    """Store *explicit* added sugar evidence; total sugar is never consulted."""
    if value_g < 0 or basis_amount <= 0 or not evidence_basis.strip():
        raise ValueError("Invalid added-sugar evidence")
    if valid_from and valid_to and valid_from > valid_to:
        raise ValueError("Invalid added-sugar evidence date range")
    if provenance in {"current_product_label", "manufacturer_exact_current"} and not valid_from:
        raise ValueError("Current product label requires a starting validity date")
    owner = c.execute(
        "SELECT canonical_food_id FROM food_formulations WHERE id=?", (formulation_id,)
    ).fetchone()
    if not owner or owner[0] != canonical_food_id:
        raise ValueError("Formulation does not belong to canonical food")
    if supersedes_id is not None:
        prior = c.execute(
            "SELECT canonical_food_id,formulation_id FROM added_sugar_evidence WHERE id=?",
            (supersedes_id,),
        ).fetchone()
        if not prior or tuple(prior) != (canonical_food_id, formulation_id):
            raise ValueError("Superseded added-sugar evidence must describe the same formulation")
    payload = {
        "canonical_food_id": canonical_food_id, "formulation_id": formulation_id,
        "value_g": value_g, "basis_amount": basis_amount, "basis_unit": basis_unit,
        "provenance": provenance, "confidence": confidence, "evidence_basis": evidence_basis,
        "evidence_url": evidence_url, "valid_from": valid_from, "valid_to": valid_to,
        "user_confirmed": int(user_confirmed), "supersedes_id": supersedes_id,
    }
    result = c.execute(
        """INSERT OR IGNORE INTO added_sugar_evidence
           (canonical_food_id,formulation_id,value_g,basis_amount,basis_unit,
            provenance,confidence,evidence_basis,evidence_url,valid_from,valid_to,
            user_confirmed,supersedes_id,content_sha256,created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (canonical_food_id, formulation_id, value_g, basis_amount, basis_unit,
         provenance, confidence, evidence_basis, evidence_url, valid_from, valid_to,
         int(user_confirmed), supersedes_id, _digest(payload), _now()),
    )
    return result.rowcount == 1


def run_added_sugar_pilot(c: sqlite3.Connection, *, start: date, end: date,
                          limit: int = 30, research_date: date | None = None) -> dict:
    """Review high-exposure identities; store only defensible explicit zeros.

    Sweetened/branded foods remain unknown until an exact dated label and
    serving relationship are verified by a human or an exact source match.
    """
    candidates = c.execute(
        """SELECT f.id,f.canonical_name,f.canonical_brand,COUNT(o.id) occurrences,
                  SUM(COALESCE(n.value,0)) source_calories
           FROM canonical_foods f JOIN food_source_identities i ON i.canonical_food_id=f.id
           JOIN food_occurrences o ON o.source_food_id=i.source_food_id
           LEFT JOIN nutrient_observations n ON n.occurrence_id=o.id AND n.nutrient='calories'
           WHERE o.is_current=1 AND o.source_date BETWEEN ? AND ?
           GROUP BY f.id ORDER BY source_calories DESC,occurrences DESC LIMIT ?""",
        (start.isoformat(), end.isoformat(), limit),
    ).fetchall()
    recorded = 0
    reviewed = []
    for food in candidates:
        name = food["canonical_name"]
        decision = "unknown_exact_product_or_recipe_not_verified"
        if name in GENERIC_ZERO_IDENTITIES and not food["canonical_brand"]:
            formulation = c.execute(
                """SELECT f.id,s.canonical_amount,s.canonical_unit FROM food_formulations f
                   JOIN formulation_servings s ON s.formulation_id=f.id
                   WHERE f.canonical_food_id=? ORDER BY f.version DESC,s.id LIMIT 1""",
                (food["id"],),
            ).fetchone()
            if formulation:
                recorded += record_added_sugar(
                    c, canonical_food_id=food["id"], formulation_id=formulation["id"],
                    value_g=0, basis_amount=formulation["canonical_amount"],
                    basis_unit=formulation["canonical_unit"],
                    provenance="USDA_generic_verified_zero", confidence="high",
                    evidence_basis=GENERIC_ZERO_IDENTITIES[name],
                    evidence_url=GENERIC_ZERO_REFERENCE,
                )
                decision = "generic_verified_zero"
        label = CURRENT_LABEL_ZEROS.get((name, food["canonical_brand"]))
        if label is not None:
            formulation = c.execute(
                "SELECT id FROM food_formulations WHERE canonical_food_id=? ORDER BY version DESC LIMIT 1",
                (food["id"],),
            ).fetchone()
            if formulation:
                existing = c.execute(
                    """SELECT 1 FROM added_sugar_evidence WHERE formulation_id=?
                       AND evidence_url=? AND provenance='manufacturer_exact_current' LIMIT 1""",
                    (formulation[0], label[2]),
                ).fetchone()
                if not existing:
                    recorded += record_added_sugar(
                        c, canonical_food_id=food["id"], formulation_id=formulation[0],
                        value_g=0, basis_amount=label[0], basis_unit=label[1],
                        provenance="manufacturer_exact_current", confidence="high",
                        evidence_basis=label[3], evidence_url=label[2],
                        valid_from=(research_date or date.today()).isoformat(),  # noqa: DTZ011
                    )
                decision = "current_label_zero_not_historical"
        reviewed.append({"canonical_food_id": food["id"], "name": name,
                         "occurrences": food["occurrences"],
                         "source_calories": round(food["source_calories"] or 0, 1),
                         "decision": decision})
    summary = {"foods_reviewed": len(reviewed), "evidence_created": recorded,
               "reviewed": reviewed}
    fingerprint = _digest({"scope_start": start.isoformat(), "scope_end": end.isoformat(),
                           "reviewed": reviewed, "pilot_version": 1})
    c.execute(
        """INSERT OR IGNORE INTO added_sugar_pilot_runs
           (scope_start,scope_end,summary_json,content_sha256,created_at)
           VALUES (?,?,?,?,?)""",
        (start.isoformat(), end.isoformat(), json.dumps(summary, sort_keys=True),
         fingerprint, _now()),
    )
    return summary


def _latest_evidence(c: sqlite3.Connection, table: str) -> dict:
    if table not in {"food_pattern_evidence", "added_sugar_evidence"}:
        raise ValueError("Unsupported evidence table")
    result = {}
    for row in c.execute(f"SELECT * FROM {table} ORDER BY id"):
        key = (row["formulation_id"], row["pattern_key"]) if table == "food_pattern_evidence" else row["formulation_id"]
        previous = result.get(key)
        if previous is None or (row["user_confirmed"], row["id"]) > (
            previous["user_confirmed"], previous["id"]
        ):
            result[key] = dict(row)
    return result


def read_pattern_period(c: sqlite3.Connection, start: date, end: date,
                        *, eligible_dates: set[str] | None = None) -> dict:
    """Return separate verified and directional evidence with explicit coverage."""
    if not c.execute("SELECT 1 FROM sqlite_master WHERE name='food_pattern_evidence'").fetchone():
        return {key: {"verified_quantity": None, "directional_occurrences": 0,
                      "meaningful_occurrences": 0, "unknown_occurrences": 0,
                      "classified_occurrences": 0, "quality": "Incomplete"} for key in PATTERNS}
    evidence = _latest_evidence(c, "food_pattern_evidence")
    by_key = {key: defaultdict(int) for key in PATTERNS}
    quantities = defaultdict(float)
    units = {}
    subgroups = defaultdict(lambda: defaultdict(int))
    plants = set()
    qualifying_foods = set()
    qualifying_food_details = {}
    rows = c.execute(
        """SELECT o.*,i.canonical_food_id,
                  (SELECT n.value FROM nutrient_observations n WHERE n.occurrence_id=o.id
                   AND n.nutrient='calories' LIMIT 1) source_calories
           FROM food_occurrences o
           LEFT JOIN food_source_identities i ON i.source='loseit' AND i.source_food_id=o.source_food_id
           WHERE o.is_current=1 AND o.source_date BETWEEN ? AND ? ORDER BY o.id""",
        (start.isoformat(), end.isoformat()),
    ).fetchall()
    rows = [row for row in rows if eligible_dates is None or row["source_date"] in eligible_dates]
    for row in rows:
        occurrence = dict(row)
        if occurrence["canonical_food_id"] is None:
            continue
        formulation = c.execute(
            """SELECT h.formulation_id FROM occurrence_resolution_history h
               WHERE h.occurrence_id=? ORDER BY h.id DESC LIMIT 1""", (occurrence["id"],)
        ).fetchone()
        if not formulation:
            continue
        fid = formulation[0]
        scale, _, _ = serving_scale(c, occurrence, fid)
        for key in PATTERNS[:-1]:
            item = evidence.get((fid, key))
            if item is None:
                by_key[key]["unknown"] += 1
                continue
            if item["valid_from"] and occurrence["source_date"] < item["valid_from"]:
                by_key[key]["unknown"] += 1
                continue
            if item["valid_to"] and occurrence["source_date"] > item["valid_to"]:
                by_key[key]["unknown"] += 1
                continue
            by_key[key]["classified"] += 1
            if item["presence_class"] in QUALIFYING:
                by_key[key]["meaningful"] += 1
                if key == "vegetable" and item["vegetable_subgroup"]:
                    subgroups[key][item["vegetable_subgroup"]] += 1
                threshold = 25 if key in {"whole_grain", "legume", "nut_seed"} else 10
                if (item["plant_identity"] and scale is not None and scale >= .25
                    and occurrence["source_calories"] is not None
                    and occurrence["source_calories"] >= threshold):
                    plants.add(item["plant_identity"])
                    qualifying_foods.add(item["plant_identity"])
                    qualifying_food_details[occurrence["canonical_food_id"]] = {
                        "canonical_food_id": occurrence["canonical_food_id"],
                        "food_name": c.execute(
                            "SELECT canonical_name FROM canonical_foods WHERE id=?",
                            (occurrence["canonical_food_id"],),
                        ).fetchone()[0],
                        "plant_identity": item["plant_identity"],
                    }
            elif item["presence_class"] == "presence_known_quantity_unknown":
                by_key[key]["directional"] += 1
            if item["quantity_kind"] == "exact_equivalent" and scale is not None:
                quantities[key] += item["equivalent_value"] * scale
                units[key] = item["equivalent_unit"]
    result = {}
    for key in PATTERNS[:-1]:
        counts = by_key[key]
        classified = counts["classified"]
        result[key] = {
            "verified_quantity": round(quantities[key], 2) if key in units else None,
            "verified_unit": units.get(key),
            "meaningful_occurrences": counts["meaningful"],
            "directional_occurrences": counts["directional"],
            "classified_occurrences": classified,
            "unknown_occurrences": counts["unknown"],
            "total_occurrences": len(rows),
            "classification_coverage_pct": round(100 * classified / len(rows), 1) if rows else None,
            "evidence_level": "Directional" if classified else "Unknown",
            "quality": "Directional" if rows and classified / len(rows) >= .5 else "Incomplete",
            "reference": PATTERN_REFERENCES[key],
            "subgroups": dict(subgroups[key]) if key == "vegetable" else {},
        }
    result["plant_variety"] = {
        "distinct_plants": len(plants), "qualifying_foods": sorted(qualifying_foods),
        "qualifying_food_details": sorted(qualifying_food_details.values(),
                                           key=lambda row: (row["plant_identity"], row["canonical_food_id"])),
        "evidence_level": "Directional" if plants else "Unknown",
        "quality": "Incomplete",
        "reference": PATTERN_REFERENCES["plant_variety"],
        "total_occurrences": len(rows),
    }
    return result


def added_sugar_period(c: sqlite3.Connection, start: date, end: date,
                       *, eligible_dates: set[str] | None = None) -> dict:
    """Coverage uses only exact formulation/portion evidence, never total sugar."""
    evidence = _latest_evidence(c, "added_sugar_evidence") if c.execute(
        "SELECT 1 FROM sqlite_master WHERE name='added_sugar_evidence'"
    ).fetchone() else {}
    rows = c.execute(
        """SELECT o.*,i.canonical_food_id,
                  (SELECT n.value FROM nutrient_observations n
                   WHERE n.occurrence_id=o.id AND n.nutrient='calories' LIMIT 1) source_calories
           FROM food_occurrences o
           LEFT JOIN food_source_identities i ON i.source='loseit' AND i.source_food_id=o.source_food_id
           WHERE o.is_current=1 AND o.source_date BETWEEN ? AND ?""",
        (start.isoformat(), end.isoformat()),
    ).fetchall()
    rows = [row for row in rows if eligible_dates is None or row["source_date"] in eligible_dates]
    known = 0
    calories = 0.0
    known_calories = 0.0
    total = 0.0
    for raw in rows:
        row = dict(raw)
        calories += row["source_calories"] or 0
        if row["canonical_food_id"] is None:
            continue
        formulation = c.execute(
            """SELECT h.formulation_id FROM occurrence_resolution_history h
               WHERE h.occurrence_id=? ORDER BY h.id DESC LIMIT 1""", (row["id"],)
        ).fetchone()
        if not formulation:
            continue
        item = evidence.get(formulation[0])
        if item is None:
            continue
        if item["valid_from"] and row["source_date"] < item["valid_from"]:
            continue
        if item["valid_to"] and row["source_date"] > item["valid_to"]:
            continue
        serving = c.execute(
            """SELECT canonical_amount,canonical_unit FROM formulation_servings
               WHERE formulation_id=? ORDER BY id LIMIT 1""", (formulation[0],)
        ).fetchone()
        factor, _, _ = serving_scale(c, row, formulation[0])
        if not serving or factor is None or serving[0] != item["basis_amount"] or serving[1] != item["basis_unit"]:
            continue
        known += 1
        known_calories += row["source_calories"] or 0
        total += item["value_g"] * factor
    occurrence_pct = 100 * known / len(rows) if rows else None
    calorie_pct = 100 * known_calories / calories if calories else None
    reportable = bool(occurrence_pct is not None and occurrence_pct >= 90 and calorie_pct is not None and calorie_pct >= 90)
    return {
        "known_total_g": round(total, 2) if known else None,
        "known_occurrences": known,
        "total_occurrences": len(rows),
        "occurrence_coverage_pct": round(occurrence_pct, 1) if occurrence_pct is not None else None,
        "source_calorie_coverage_pct": round(calorie_pct, 1) if calorie_pct is not None else None,
        "reportable": reportable,
        "quality": "Mostly reliable" if reportable else "Incomplete",
    }


def weekly_audit(data_dir: Path, *, today: date | None = None,
                 research_provider=None) -> dict:
    """Research queue refresh; safe without a network provider or AI service."""
    today = today or date.today()  # noqa: DTZ011
    start = today - timedelta(days=29)
    with NutritionRepository(data_dir) as repo, repo.connection as c:
        classified = classify_obvious(c)
        unresolved = c.execute(
            """SELECT f.id,f.canonical_name,f.canonical_brand,
                      (SELECT MAX(version) FROM food_formulations WHERE canonical_food_id=f.id) formulation_version,
                      COUNT(o.id) n,SUM(COALESCE(nutr.value,0)) source_calories FROM canonical_foods f
               JOIN food_source_identities i ON i.canonical_food_id=f.id
               JOIN food_occurrences o ON o.source_food_id=i.source_food_id
               LEFT JOIN nutrient_observations nutr ON nutr.occurrence_id=o.id AND nutr.nutrient='calories'
               WHERE o.is_current=1 AND o.source_date BETWEEN ? AND ?
               AND NOT EXISTS (SELECT 1 FROM food_pattern_evidence e
                               WHERE e.canonical_food_id=f.id)
               GROUP BY f.id ORDER BY source_calories DESC,n DESC,f.id LIMIT 30""",
            (start.isoformat(), today.isoformat()),
        ).fetchall()
        researched = 0
        provider_failures = 0
        outcomes = {}
        prior_outcomes = {}
        for audit_row in c.execute("SELECT summary_json FROM food_pattern_audit_runs ORDER BY id"):
            prior_outcomes.update(json.loads(audit_row[0]).get("research_outcomes", {}))
        outcomes.update({f"{row[0]}:{row[3]}": prior_outcomes[f"{row[0]}:{row[3]}"]
                         for row in unresolved if prior_outcomes.get(f"{row[0]}:{row[3]}")
                         in {"no_match", "classified"}})
        if research_provider is not None:
            for row in unresolved:
                identity = f"{row[0]}:{row[3]}"
                if prior_outcomes.get(identity) in {"no_match", "classified"}:
                    continue
                try:
                    assertions = research_provider({"canonical_food_id": row[0],
                                                     "name": row[1], "brand": row[2],
                                                     "occurrences": row[4],
                                                     "source_calories": row[5]})
                    outcomes[identity] = "classified" if assertions else "no_match"
                    for assertion in assertions or ():
                        formulation = c.execute(
                            "SELECT id FROM food_formulations WHERE canonical_food_id=? ORDER BY version DESC LIMIT 1",
                            (row[0],),
                        ).fetchone()
                        if formulation is None:
                            continue
                        researched += record_pattern_evidence(
                            c, canonical_food_id=row[0], formulation_id=formulation[0],
                            pattern_key=assertion["pattern_key"],
                            presence_class=assertion["presence_class"],
                            provenance=assertion["provenance"],
                            confidence=assertion["confidence"],
                            evidence_basis=assertion["evidence_basis"],
                            evidence_url=assertion.get("evidence_url"),
                            plant_identity=assertion.get("plant_identity"),
                        )
                except Exception:  # noqa: BLE001 - provider failures must not break local audit
                    provider_failures += 1
                    outcomes[identity] = "failed"
        fingerprint = _digest({"week": today.isocalendar()[:2],
                               "unresolved": [r[0] for r in unresolved],
                               "outcomes": outcomes})
        summary = {**classified, "unresolved_recent_foods": len(unresolved),
                   "research_candidates": [{"canonical_food_id": r[0], "name": r[1],
                                            "occurrences": r[4],
                                            "source_calories": round(r[5] or 0, 1)} for r in unresolved],
                   "research_classifications_created": researched,
                   "research_outcomes": outcomes,
                   "provider_failures": provider_failures,
                   "provider_status": "local_only_external_research_disabled" if research_provider is None else
                   "failed" if provider_failures else "complete"}
        c.execute(
            """INSERT OR IGNORE INTO food_pattern_audit_runs
               (scope_start,scope_end,status,summary_json,run_fingerprint,created_at)
               VALUES (?,?,'complete',?,?,?)""",
            (start.isoformat(), today.isoformat(), json.dumps(summary, sort_keys=True),
             fingerprint, _now()),
        )
        return summary
