"""Resolved Food Library, serving reuse, anomaly audits, and combined nutrition."""

from __future__ import annotations

import hashlib
import json
import math
import statistics
from collections import Counter, defaultdict
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from .repository import (
    SOURCE,
    STANDARD_NUTRIENTS,
    NutritionRepository,
    _now,
    normalize_text,
    nutrient_unit,
)

EVIDENCE_CLASSES = frozenset(
    {
        "source",
        "user_confirmed",
        "researched_exact",
        "researched_historical_exact",
        "researched_strong_match",
        "representative_estimate",
        "modeled_estimate",
    }
)
CONFIDENCE_LEVELS = frozenset({"very_high", "high", "medium", "low"})
ANIMAL_WORDS = {
    "beef", "chicken", "egg", "eggs", "sausage", "salmon", "shrimp", "meatloaf",
    "chorizo", "wing", "wings", "frank", "franks", "fish", "cheese", "queso",
}


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: Any) -> str:
    return hashlib.sha256(_json(value).encode()).hexdigest()


# Values are per canonical serving. Partial records intentionally inherit coherent
# source values and only replace/fill the named fields.
KNOWN_PROFILES: dict[str, dict[str, Any]] = {
    "4f53d67a020b659a73482c6a699e2e1b": {
        "unit": "cookie", "logged_unit": "serving", "mapping": 1,
        "evidence": "researched_exact", "confidence": "high",
        "reference": "User-supplied exact Walmart historical product reconciliation",
        "nutrients": {"calories": 160, "protein_g": 1, "carb_g": 26, "total_fat_g": 6,
                      "saturated_fat_g": 3, "fiber_g": 0, "sugar_g": 16,
                      "sodium_mg": 105, "cholesterol_mg": 5},
    },
    "a0110e80441a8da0be44c5ee5d044eb3": {
        "unit": "container", "evidence": "researched_exact", "confidence": "high",
        "reference": "Chick-fil-A BBQ Sauce exact product profile",
        "nutrients": {"calories": 45, "protein_g": 0, "carb_g": 11, "total_fat_g": 0,
                      "saturated_fat_g": 0, "fiber_g": 0, "sugar_g": 9,
                      "sodium_mg": 200, "cholesterol_mg": 0},
    },
    "49c67296822d7b85b94fd3793a3e8465": {
        "unit": "cone", "logged_unit": "piece", "mapping": 2,
        "evidence": "user_confirmed", "confidence": "very_high",
        "reference": "User-confirmed mapping: one Lose It piece equals two Oreo mini cones",
        "nutrients": {"calories": 110, "protein_g": 1, "carb_g": 18, "total_fat_g": 4,
                      "saturated_fat_g": 3, "fiber_g": .5, "sugar_g": 11.5,
                      "sodium_mg": 52.5, "cholesterol_mg": 0},
    },
    "f96a4d4da2c2c98d064aac70c45fd3ee": {
        "unit": "slice", "logged_unit": "slice", "mapping": 2,
        "evidence": "user_confirmed", "confidence": "very_high",
        "reference": "User-confirmed historical mapping: two logged slices represent four actual slices",
        "nutrients": {"calories": 120, "protein_g": 4, "carb_g": 21, "total_fat_g": 2.5,
                      "saturated_fat_g": 0, "fiber_g": 2, "sugar_g": 3,
                      "sodium_mg": 150, "cholesterol_mg": 0},
    },
    "9bd78ee97aa157a8d344c701fae7b703": {
        "amount": 25, "unit": "g", "mapping": 25,
        "evidence": "researched_exact", "confidence": "high",
        "reference": "Pop Secret Homestyle Family Bag label, 25 g unpopped",
        "nutrients": {"calories": 130, "protein_g": 2, "carb_g": 12, "total_fat_g": 8,
                      "saturated_fat_g": 4, "fiber_g": 2, "sugar_g": 0,
                      "sodium_mg": 290, "cholesterol_mg": 0},
    },
    "d17131605c1aa0b03243fbd9eb6aaba0": {
        "unit": "regular", "evidence": "researched_strong_match", "confidence": "high",
        "reference": "AMC regular popcorn, no butter, matched historical profile",
        "nutrients": {"calories": 550, "protein_g": 12, "carb_g": 74, "total_fat_g": 24,
                      "saturated_fat_g": 2, "fiber_g": 14, "sugar_g": 0,
                      "sodium_mg": 1380, "cholesterol_mg": 0},
    },
    "7e344e7478e8dea26340ac1d8d15c33c": {
        "amount": 4, "unit": "oz", "mapping": 4,
        "evidence": "researched_exact", "confidence": "high",
        "reference": "Chipotle queso exact 4 oz historical profile",
        "nutrients": {"calories": 240, "protein_g": 10, "carb_g": 7, "total_fat_g": 18,
                      "saturated_fat_g": 12, "fiber_g": 0, "sugar_g": 2,
                      "sodium_mg": 490, "cholesterol_mg": 60},
    },
    "6b1f8c4b4202b99c684f751f98ae654f": {
        "unit": "waffle", "evidence": "researched_historical_exact", "confidence": "high",
        "reference": "Waffle House classic waffle matched historical product profile",
        "nutrients": {"calories": 410, "protein_g": 8, "carb_g": 55, "total_fat_g": 18,
                      "saturated_fat_g": 10, "fiber_g": 2, "sugar_g": 15,
                      "sodium_mg": 870, "cholesterol_mg": 50},
    },
    "67bb2d82a548e1963b41a47a1da96af0": {
        "unit": "serving", "evidence": "researched_historical_exact", "confidence": "high",
        "reference": "Fazoli's historical 930-calorie formulation",
        "nutrients": {"calories": 930, "protein_g": 38, "carb_g": 116, "total_fat_g": 35,
                      "saturated_fat_g": 17, "fiber_g": 9, "sugar_g": 14,
                      "sodium_mg": 2400, "cholesterol_mg": 105},
    },
    "7a33c5b089aa04a10a42d690d7dfaca0": {
        "unit": "serving", "evidence": "researched_historical_exact", "confidence": "high",
        "reference": "Sonny's official historical baked beans profile",
        "nutrients": {"calories": 240, "protein_g": 8, "carb_g": 49, "total_fat_g": 4.5,
                      "saturated_fat_g": 0, "fiber_g": 5, "sugar_g": 28,
                      "sodium_mg": 900, "cholesterol_mg": 5},
    },
    "10e258f32db4c289fd478020a46c5af6": {
        "unit": "serving", "evidence": "researched_historical_exact", "confidence": "high",
        "reference": "Sonic chili add-on matched historical profile",
        "nutrients": {"calories": 50, "protein_g": 3, "carb_g": 2, "total_fat_g": 3.5,
                      "saturated_fat_g": 1.5, "fiber_g": 1, "sugar_g": 0,
                      "sodium_mg": 160, "cholesterol_mg": 10},
    },
    "f0e5f283347d4cad3140e9b34bf8e2a8": {
        "unit": "serving", "evidence": "researched_historical_exact", "confidence": "high",
        "reference": "McDonald's medium McCafé iced mocha historical 320-calorie profile",
        "nutrients": {"calories": 320, "protein_g": 8, "carb_g": 48, "total_fat_g": 11,
                      "saturated_fat_g": 7, "fiber_g": 2, "sugar_g": 43,
                      "sodium_mg": 125, "cholesterol_mg": 35},
    },
    "1655fc0418878d9d8f4c9fb40b1cb35c": {
        "unit": "serving", "evidence": "modeled_estimate", "confidence": "medium",
        "reference": "Calorie-constrained Taco Mama chorizo soft taco representative model",
        "nutrients": {"calories": 320, "protein_g": 14, "carb_g": 30, "total_fat_g": 16,
                      "saturated_fat_g": 6, "fiber_g": 4, "sugar_g": 2,
                      "sodium_mg": 800, "cholesterol_mg": 45},
    },
    "23b28e24b7ee76a29941a19cd515725f": {
        "amount": 4, "unit": "fluid_ounce", "evidence": "modeled_estimate", "confidence": "medium",
        "reference": "640-calorie anchored representative chicken-noodle-soup model",
        "nutrients": {"calories": 640, "protein_g": 53.6, "carb_g": 72.8, "total_fat_g": 14,
                      "saturated_fat_g": 4, "fiber_g": 5, "sugar_g": 8,
                      "sodium_mg": 1200, "cholesterol_mg": 80},
    },
    "d6ee11b3102a8fa02e46728f7242c1a1": {
        "amount": 2, "unit": "piece", "evidence": "researched_strong_match", "confidence": "medium",
        "reference": "90-calorie anchored Twix Minis representative product profile",
        "nutrients": {"calories": 90, "protein_g": 1, "carb_g": 12, "total_fat_g": 4,
                      "saturated_fat_g": 2.5, "fiber_g": 0, "sugar_g": 9,
                      "sodium_mg": 35, "cholesterol_mg": 0},
    },
}

PARTIAL_OVERRIDES: dict[str, dict[str, float]] = {
    "e8f95698a5b87686eb49f96e524c1139": {"saturated_fat_g": 0},
    "0ff7448c5f877f88ca491c0f9dfdfefb": {"saturated_fat_g": 0, "cholesterol_mg": 0},
    "be27d3af2af633980e43c87860c3ee25": {"fiber_g": 0, "cholesterol_mg": 0},
    "9a0ad656f9207a94bf443ae5a459beb9": {"sodium_mg": 130, "cholesterol_mg": 0},
    "fb895d390f434d8f37432f270a03a0b2": {"cholesterol_mg": 0},
    "56ff2d45d6b26cb599466eccbe770834": {"cholesterol_mg": 5},
}

CANONICAL_ALIASES = {
    "b0f04281941eab915044a70b51692c24": "8134c5320a6c1f829941230cefc2aa44",
}


def _finite(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)


def _nutrients(c, occurrence_id: int) -> dict[str, float]:
    return {
        row["nutrient"]: float(row["value"])
        for row in c.execute(
            "SELECT nutrient,value FROM nutrient_observations WHERE occurrence_id=? AND provenance='loseit'",
            (occurrence_id,),
        )
        if row["nutrient"] in STANDARD_NUTRIENTS and _finite(row["value"])
    }


def detect_anomalies(nutrients: dict[str, float], *, amount=None, unit=None) -> list[dict]:
    findings: list[dict] = []
    calories = nutrients.get("calories")
    for nutrient, value in nutrients.items():
        if value < 0:
            findings.append({"code": "negative_nutrient", "severity": "error", "nutrients": [nutrient]})
    for subset, total in (
        ("saturated_fat_g", "total_fat_g"), ("fiber_g", "carb_g"), ("sugar_g", "carb_g")
    ):
        if subset in nutrients and total in nutrients and nutrients[subset] > nutrients[total] + 1:
            findings.append(
                {"code": f"{subset}_exceeds_{total}", "severity": "error", "nutrients": [subset]}
            )
    macros = {key: nutrients[key] for key in ("protein_g", "carb_g", "total_fat_g") if key in nutrients}
    if _finite(calories) and calories >= 0 and macros:
        energies = {"protein_g": macros.get("protein_g", 0) * 4,
                    "carb_g": macros.get("carb_g", 0) * 4,
                    "total_fat_g": macros.get("total_fat_g", 0) * 9}
        macro_energy = sum(energies.values())
        dominant = [key for key, energy in energies.items() if energy > max(100, calories * 2)]
        if dominant:
            findings.append(
                {"code": "impossible_single_macro_energy", "severity": "error",
                 "nutrients": dominant, "calories": calories, "macro_energy": macro_energy}
            )
        elif macro_energy > max(calories + 100, calories * 1.5):
            findings.append(
                {"code": "calorie_macro_contradiction", "severity": "error",
                 "nutrients": sorted(macros), "calories": calories, "macro_energy": macro_energy}
            )
        elif macro_energy > max(calories + 60, calories * 1.25):
            findings.append(
                {"code": "calorie_macro_rounding_warning", "severity": "warning",
                 "nutrients": [], "calories": calories, "macro_energy": macro_energy}
            )
    if (
        _finite(calories)
        and _finite(amount)
        and amount > 0
        and str(unit).casefold() in {"fl oz", "oz"}
        and calories / amount > 120
    ):
        findings.append(
            {"code": "extreme_energy_density_for_logged_unit", "severity": "warning",
             "nutrients": [], "calories_per_unit": calories / amount, "unit": unit}
        )
    return findings


def invalid_nutrients(nutrients: dict[str, float], *, amount=None, unit=None) -> set[str]:
    return {
        nutrient
        for finding in detect_anomalies(nutrients, amount=amount, unit=unit)
        if finding["severity"] == "error"
        for nutrient in finding["nutrients"]
    }


def _macro_shares(name: str) -> tuple[float, float, float]:
    words = set(normalize_text(name).split())
    if words & ANIMAL_WORDS:
        return (0.30, 0.15, 0.55)
    if words & {"cookie", "cookies", "cake", "cupcake", "croissant", "twix", "oreo", "syrup"}:
        return (0.06, 0.59, 0.35)
    if words & {"sauce", "salsa", "rice", "beans", "bread", "bun", "toast"}:
        return (0.10, 0.72, 0.18)
    if words & {"soup", "taco", "tacos", "burrito", "quesadilla", "pasta", "rigatoni"}:
        return (0.22, 0.48, 0.30)
    return (0.18, 0.52, 0.30)


def _complete_profile(name: str, profile: dict[str, float]) -> dict[str, tuple[float, str, str]]:
    result = {key: (max(0.0, value), "source", "high") for key, value in profile.items()}
    calories = max(0.0, profile.get("calories", 0.0))
    protein_share, carb_share, fat_share = _macro_shares(name)
    defaults = {
        "protein_g": calories * protein_share / 4,
        "carb_g": calories * carb_share / 4,
        "total_fat_g": calories * fat_share / 9,
    }
    known_energy = sum(
        profile.get(key, 0) * factor
        for key, factor in (("protein_g", 4), ("carb_g", 4), ("total_fat_g", 9))
    )
    missing_macros = [key for key in defaults if key not in profile]
    remaining = max(0.0, calories - known_energy)
    weights = {"protein_g": protein_share, "carb_g": carb_share, "total_fat_g": fat_share}
    total_weight = sum(weights[key] for key in missing_macros) or 1
    factors = {"protein_g": 4, "carb_g": 4, "total_fat_g": 9}
    for key in missing_macros:
        estimate = remaining * weights[key] / total_weight / factors[key] if calories else 0
        result[key] = (round(estimate, 3), "modeled_estimate", "low")
    fat = result.get("total_fat_g", (0, "", ""))[0]
    carbs = result.get("carb_g", (0, "", ""))[0]
    words = set(normalize_text(name).split())
    defaults_other = {
        "saturated_fat_g": min(fat, fat * (0.45 if words & {"cheese", "butter", "chocolate"} else 0.3)),
        "fiber_g": max(0.0, carbs * (0.08 if words & {"beans", "vegetable", "barley"} else 0.04)),
        "sugar_g": max(0.0, carbs * (0.5 if words & {"cookie", "cake", "cupcake", "chocolate", "syrup"} else 0.2)),
        "sodium_mg": calories * (2.0 if words & {"restaurant", "taco", "burrito", "fries", "soup", "sauce"} else 0.8),
        "cholesterol_mg": calories * 0.12 if words & ANIMAL_WORDS else 0.0,
    }
    for key, value in defaults_other.items():
        if key not in result:
            result[key] = (round(value, 3), "representative_estimate", "low")
    if "calories" not in result:
        result["calories"] = (round(known_energy, 3), "modeled_estimate", "low")
    return result


def _source_groups(c) -> dict[str, list[dict]]:
    groups: dict[str, list[dict]] = defaultdict(list)
    for row in c.execute(
        "SELECT * FROM food_occurrences WHERE is_current=1 AND source_food_id IS NOT NULL "
        "AND source_food_id!='' ORDER BY id"
    ):
        item = dict(row)
        item["nutrients"] = _nutrients(c, row["id"])
        groups[row["source_food_id"]].append(item)
    return groups


def _dominant_unit(rows: list[dict]) -> str:
    return Counter(str(row["unit"] or "serving") for row in rows).most_common(1)[0][0]


def _median_source_profile(rows: list[dict], *, multiplier=1.0) -> tuple[str, dict[str, float]]:
    unit = _dominant_unit(rows)
    values: dict[str, list[float]] = defaultdict(list)
    for row in rows:
        if str(row["unit"] or "serving") != unit:
            continue
        amount = row["amount"] if _finite(row["amount"]) and row["amount"] > 0 else 1.0
        invalid = invalid_nutrients(row["nutrients"], amount=row["amount"], unit=row["unit"])
        for nutrient, value in row["nutrients"].items():
            if nutrient not in invalid:
                values[nutrient].append(value / (amount * multiplier))
    return unit, {key: statistics.median(points) for key, points in values.items() if points}


def _catalog_signature(source_food_id: str, known: dict) -> str:
    return _digest({"source_food_id": source_food_id, "catalog": known})


def _upgrade_known_profiles(c, groups: dict[str, list[dict]]) -> int:
    """Append corrected catalog formulations without rewriting earlier versions."""
    added = 0
    for source_food_id, known in KNOWN_PROFILES.items():
        if source_food_id not in groups:
            continue
        identity = c.execute(
            "SELECT canonical_food_id FROM food_source_identities WHERE source=? AND source_food_id=?",
            (SOURCE, source_food_id),
        ).fetchone()
        if not identity:
            continue
        signature = _catalog_signature(source_food_id, known)
        latest = c.execute(
            "SELECT * FROM food_formulations WHERE canonical_food_id=? ORDER BY version DESC LIMIT 1",
            (identity[0],),
        ).fetchone()
        if latest and signature in latest["assumptions"]:
            continue
        version = (latest["version"] if latest else 0) + 1
        occurrence = groups[source_food_id][-1]
        cursor = c.execute(
            "INSERT INTO food_formulations(canonical_food_id,version,formulation_name,historical,source_reference,reference_url,assumptions,created_at) VALUES (?,?,?,?,?,?,?,?)",
            (identity[0], version,
             occurrence["food_name_raw"] or occurrence["food_name_normalized"],
             int("historical" in known["evidence"] or source_food_id == "f96a4d4da2c2c98d064aac70c45fd3ee"),
             known["reference"], known.get("url"),
             "Catalog signature " + signature + "; prior formulations remain immutable.", _now()),
        )
        formulation_id = int(cursor.lastrowid)
        logged_unit = str(known.get("logged_unit") or _dominant_unit(groups[source_food_id]))
        c.execute(
            "INSERT INTO formulation_servings(formulation_id,canonical_amount,canonical_unit,gram_weight,logged_unit,actual_units_per_logged_unit,mapping_type,confidence,evidence,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (formulation_id, float(known.get("amount", 1)), str(known.get("unit") or logged_unit),
             known.get("gram_weight"), logged_unit, float(known.get("mapping", 1)),
             "user_confirmed" if known["evidence"] == "user_confirmed" else "explicit_same_unit",
             known["confidence"], known["reference"], _now()),
        )
        for nutrient, value in known["nutrients"].items():
            c.execute(
                "INSERT INTO formulation_nutrients(formulation_id,nutrient,value,unit,provenance,confidence,evidence,created_at) VALUES (?,?,?,?,?,?,?,?)",
                (formulation_id, nutrient, float(value), nutrient_unit(nutrient), known["evidence"],
                 known["confidence"], known["reference"], _now()),
            )
        added += 1
    return added


def _add_safe_alias_servings(c, groups: dict[str, list[dict]]) -> int:
    added = 0
    for alias_id, primary_id in CANONICAL_ALIASES.items():
        if alias_id not in groups or primary_id not in groups:
            continue
        formulation = _latest_formulation(c, primary_id)
        if not formulation:
            continue
        serving = c.execute(
            "SELECT * FROM formulation_servings WHERE formulation_id=? ORDER BY id LIMIT 1",
            (formulation["id"],),
        ).fetchone()
        calories = c.execute(
            "SELECT value FROM formulation_nutrients WHERE formulation_id=? AND nutrient='calories'",
            (formulation["id"],),
        ).fetchone()
        if not serving or not calories or not calories[0]:
            continue
        logged_unit, alias_profile = _median_source_profile(groups[alias_id])
        alias_calories = alias_profile.get("calories")
        if alias_calories is None or abs(alias_calories - calories[0]) > max(10, calories[0] * .1):
            continue
        before = c.total_changes
        c.execute(
            "INSERT OR IGNORE INTO formulation_servings(formulation_id,canonical_amount,canonical_unit,gram_weight,logged_unit,actual_units_per_logged_unit,mapping_type,confidence,evidence,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (formulation["id"], serving["canonical_amount"], serving["canonical_unit"],
             serving["gram_weight"], logged_unit, serving["canonical_amount"],
             "fingerprint_confirmed_alias", "high",
             f"Per-unit calorie fingerprint matches canonical source ID {primary_id}.", _now()),
        )
        added += c.total_changes - before
    return added


def populate_library(data_dir: Path) -> dict[str, int]:
    """Idempotently create canonical foods/formulations for all stable source IDs."""
    result = {"source_ids_seen": 0, "canonical_foods_created": 0, "formulations_created": 0,
              "nutrients_created": 0, "aliases_reused": 0, "occurrences_resolved": 0}
    with NutritionRepository(data_dir) as repo, repo.connection as c:
        groups = _source_groups(c)
        result["source_ids_seen"] = len(groups)
        canonical_by_source = {
            row["source_food_id"]: row["canonical_food_id"]
            for row in c.execute("SELECT source_food_id,canonical_food_id FROM food_source_identities")
        }
        for source_food_id in sorted(groups, key=lambda value: (value in CANONICAL_ALIASES, value)):
            rows = groups[source_food_id]
            if source_food_id in canonical_by_source:
                continue
            alias_of = CANONICAL_ALIASES.get(source_food_id)
            canonical_id = canonical_by_source.get(alias_of) if alias_of else None
            latest = rows[-1]
            if canonical_id is None:
                cursor = c.execute(
                    "INSERT INTO canonical_foods(canonical_name,canonical_brand,created_at) VALUES (?,?,?)",
                    (latest["food_name_raw"] or latest["food_name_normalized"],
                     latest["brand_raw"] or latest["brand_normalized"], _now()),
                )
                canonical_id = int(cursor.lastrowid)
                result["canonical_foods_created"] += 1
            else:
                result["aliases_reused"] += 1
            evidence = "researched_strong_match" if alias_of else "source"
            c.execute(
                "INSERT INTO food_source_identities(canonical_food_id,source,source_food_id,evidence_class,confidence,note,created_at) VALUES (?,?,?,?,?,?,?)",
                (canonical_id, SOURCE, source_food_id, evidence, "high",
                 f"Explicit canonical alias of {alias_of}" if alias_of else "Stable Lose It source identity", _now()),
            )
            canonical_by_source[source_food_id] = canonical_id
            if alias_of:
                continue

            known = KNOWN_PROFILES.get(source_food_id, {})
            multiplier = float(known.get("mapping", 1))
            logged_unit, source_profile = _median_source_profile(rows, multiplier=multiplier)
            if known.get("nutrients"):
                profile = _complete_profile(latest["food_name_raw"], source_profile)
                for nutrient, value in known["nutrients"].items():
                    profile[nutrient] = (
                        float(value), known["evidence"], known["confidence"]
                    )
            else:
                profile = _complete_profile(latest["food_name_raw"], source_profile)
                for nutrient, value in PARTIAL_OVERRIDES.get(source_food_id, {}).items():
                    profile[nutrient] = (float(value), "researched_exact", "high")
            amount = float(known.get("amount", 1))
            canonical_unit = str(known.get("unit") or logged_unit)
            formulation_evidence = str(known.get("evidence") or "source")
            historical = int("historical" in formulation_evidence or source_food_id in {
                "f96a4d4da2c2c98d064aac70c45fd3ee", "4f694ee36780e8bd5241f6f31623ec00",
                "99e663c3b68a2883284f68c08082b644", "aa05e8192b611c9c534980e3369ba9ab",
            })
            cursor = c.execute(
                "INSERT INTO food_formulations(canonical_food_id,version,formulation_name,historical,source_reference,reference_url,assumptions,created_at) VALUES (?,1,?,?,?,?,?,?)",
                (canonical_id, latest["food_name_raw"] or latest["food_name_normalized"], historical,
                 known.get("reference") or "Resolved from coherent Lose It source fingerprint with conservative estimation for missing fields",
                 known.get("url"),
                 ("Catalog signature " + _catalog_signature(source_food_id, known)
                  if known else "Valid source observations remain authoritative; estimated fields are nutrient-level and explicitly labeled."),
                 _now()),
            )
            formulation_id = int(cursor.lastrowid)
            result["formulations_created"] += 1
            c.execute(
                "INSERT INTO formulation_servings(formulation_id,canonical_amount,canonical_unit,gram_weight,logged_unit,actual_units_per_logged_unit,mapping_type,confidence,evidence,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (formulation_id, amount, canonical_unit, known.get("gram_weight"),
                 str(known.get("logged_unit") or logged_unit), multiplier,
                 "user_confirmed" if formulation_evidence == "user_confirmed" else "explicit_same_unit",
                 known.get("confidence", "high"), known.get("reference") or "Observed Lose It serving unit", _now()),
            )
            for nutrient in STANDARD_NUTRIENTS:
                value, provenance, confidence = profile[nutrient]
                c.execute(
                    "INSERT INTO formulation_nutrients(formulation_id,nutrient,value,unit,provenance,confidence,evidence,created_at) VALUES (?,?,?,?,?,?,?,?)",
                    (formulation_id, nutrient, value, nutrient_unit(nutrient), provenance,
                     confidence, known.get("reference") or "Source-anchored conservative resolution", _now()),
                )
                result["nutrients_created"] += 1
        result["catalog_formulations_upgraded"] = _upgrade_known_profiles(c, groups)
        result["alias_serving_mappings_added"] = _add_safe_alias_servings(c, groups)
        result["occurrences_resolved"] = refresh_occurrence_resolutions(c)
        reconcile_review_state(c)
    return result


def _latest_formulation(c, source_food_id: str):
    return c.execute(
        "SELECT f.* FROM food_source_identities i JOIN food_formulations f ON f.canonical_food_id=i.canonical_food_id WHERE i.source=? AND i.source_food_id=? ORDER BY f.version DESC,f.id DESC LIMIT 1",
        (SOURCE, source_food_id),
    ).fetchone()


def serving_scale(c, occurrence: dict, formulation_id: int) -> tuple[float | None, str | None, str | None]:
    servings = c.execute(
        "SELECT * FROM formulation_servings WHERE formulation_id=? ORDER BY id", (formulation_id,)
    ).fetchall()
    amount = occurrence.get("amount")
    unit = str(occurrence.get("unit") or "")
    if not _finite(amount) or amount < 0:
        return None, None, None
    for serving in servings:
        if unit == serving["logged_unit"]:
            factor = amount * serving["actual_units_per_logged_unit"] / serving["canonical_amount"]
            return factor, "logged_unit_mapping", serving["confidence"]
        if unit == serving["canonical_unit"]:
            return amount / serving["canonical_amount"], "canonical_unit", serving["confidence"]
        if serving["gram_weight"] and unit.casefold() in {"g", "gram", "grams"}:
            return amount / serving["gram_weight"], "authoritative_gram_weight", serving["confidence"]
    return None, None, None


def refresh_occurrence_resolutions(c) -> int:
    added = 0
    for row in c.execute(
        "SELECT * FROM food_occurrences WHERE is_current=1 AND source_food_id IS NOT NULL ORDER BY id"
    ).fetchall():
        formulation = _latest_formulation(c, row["source_food_id"])
        if not formulation:
            continue
        factor, method, confidence = serving_scale(c, dict(row), formulation["id"])
        if factor is None:
            continue
        payload = {"occurrence_id": row["id"], "formulation_id": formulation["id"],
                   "scale_factor": factor, "mapping_method": method, "confidence": confidence}
        before = c.total_changes
        c.execute(
            "INSERT OR IGNORE INTO occurrence_resolution_history(occurrence_id,formulation_id,scale_factor,mapping_method,confidence,content_sha256,created_at) VALUES (?,?,?,?,?,?,?)",
            (row["id"], formulation["id"], factor, method, confidence, _digest(payload), _now()),
        )
        added += c.total_changes - before
    return added


def library_values_for_occurrence(c, occurrence: dict) -> dict[str, dict]:
    if not occurrence.get("source_food_id"):
        return {}
    formulation = _latest_formulation(c, occurrence["source_food_id"])
    if not formulation:
        return {}
    factor, method, mapping_confidence = serving_scale(c, occurrence, formulation["id"])
    if factor is None:
        return {}
    result = {}
    for row in c.execute(
        "SELECT nutrient,value,unit,provenance,confidence,evidence FROM formulation_nutrients WHERE formulation_id=?",
        (formulation["id"],),
    ):
        result[row["nutrient"]] = {
            "value": row["value"] * factor,
            "unit": row["unit"],
            "provenance": row["provenance"],
            "confidence": row["confidence"],
            "evidence": row["evidence"],
            "formulation_id": formulation["id"],
            "scale_factor": factor,
            "mapping_method": method,
            "mapping_confidence": mapping_confidence,
        }
    return result


def _current_invalid(c, occurrence_id: int, nutrients: dict[str, float]) -> set[str]:
    invalid = set()
    for row in c.execute(
        "SELECT nutrient,source_value,status FROM source_nutrient_quality_findings WHERE occurrence_id=? ORDER BY id",
        (occurrence_id,),
    ):
        if row["status"] == "invalid" and row["nutrient"] in nutrients and math.isclose(
            row["source_value"], nutrients[row["nutrient"]], rel_tol=0, abs_tol=1e-9
        ):
            invalid.add(row["nutrient"])
    return invalid


def combined_values_for_occurrence(c, occurrence: dict) -> dict[str, dict]:
    source = _nutrients(c, occurrence["id"])
    invalid = _current_invalid(c, occurrence["id"], source)
    library = library_values_for_occurrence(c, occurrence)
    result = {}
    for nutrient in STANDARD_NUTRIENTS:
        if nutrient in source and nutrient not in invalid:
            result[nutrient] = {"value": source[nutrient], "unit": nutrient_unit(nutrient),
                                "provenance": "source", "confidence": "high",
                                "source_valid": True}
        elif nutrient in library:
            result[nutrient] = library[nutrient] | {
                "source_valid": nutrient not in invalid,
                "supersedes_invalid_source_value": source.get(nutrient) if nutrient in invalid else None,
            }
    return result


def _write_finding(c, audit_id: int, occurrence: dict, finding: dict) -> tuple[int, int]:
    payload = {"occurrence_id": occurrence["id"], **finding,
               "source_nutrients": occurrence["nutrients"]}
    digest = _digest(payload)
    before = c.total_changes
    c.execute(
        "INSERT OR IGNORE INTO nutrition_anomaly_findings(audit_run_id,occurrence_id,code,severity,details_json,content_sha256,created_at) VALUES (?,?,?,?,?,?,?)",
        (audit_id, occurrence["id"], finding["code"], finding["severity"], _json(finding), digest, _now()),
    )
    anomaly_added = c.total_changes - before
    quality_added = 0
    if finding["severity"] == "error":
        affected = set(finding["nutrients"])
        if "total_fat_g" in affected:
            affected |= {"saturated_fat_g"} & occurrence["nutrients"].keys()
        if "carb_g" in affected:
            affected |= {"fiber_g", "sugar_g"} & occurrence["nutrients"].keys()
        for nutrient in sorted(affected):
            if nutrient not in occurrence["nutrients"]:
                continue
            quality = {"occurrence_id": occurrence["id"], "nutrient": nutrient,
                       "source_value": occurrence["nutrients"][nutrient],
                       "status": "invalid", "reason": finding["code"]}
            before = c.total_changes
            c.execute(
                "INSERT OR IGNORE INTO source_nutrient_quality_findings(audit_run_id,occurrence_id,nutrient,source_value,status,reason,content_sha256,created_at) VALUES (?,?,?,?,?,?,?,?)",
                (audit_id, occurrence["id"], nutrient, quality["source_value"], "invalid",
                 finding["code"], _digest(quality), _now()),
            )
            quality_added += c.total_changes - before
    return anomaly_added, quality_added


def run_audit(data_dir: Path, cadence: str, *, start: date | None = None,
              end: date | None = None, force=False) -> dict:
    if cadence not in {"daily", "weekly", "monthly", "full_history"}:
        raise ValueError("Unsupported nutrition audit cadence")
    today = end or date.today()  # noqa: DTZ011
    with NutritionRepository(data_dir) as repo, repo.connection as c:
        latest = c.execute(
            "SELECT completed_at,scope_end FROM nutrition_audit_runs WHERE cadence=? AND status='complete' ORDER BY id DESC LIMIT 1",
            (cadence,),
        ).fetchone()
        if latest and not force:
            prior = date.fromisoformat(latest[1] or latest[0][:10])
            same_period = (
                prior == today if cadence == "daily" else
                prior.isocalendar()[:2] == today.isocalendar()[:2] if cadence == "weekly" else
                (prior.year, prior.month) == (today.year, today.month)
            )
            if same_period:
                return {"status": "already_completed", "cadence": cadence, "completed_at": latest[0]}
        if cadence == "daily":
            start = start or today - timedelta(days=7)
        elif cadence == "weekly":
            start = start or today - timedelta(days=90)
        elif cadence in {"monthly", "full_history"}:
            start = start or date(today.year, 1, 1)
        cursor = c.execute(
            "INSERT INTO nutrition_audit_runs(cadence,scope_start,scope_end,status,started_at) VALUES (?,?,?,'running',?)",
            (cadence, start.isoformat(), today.isoformat(), _now()),
        )
        audit_id = int(cursor.lastrowid)
        occurrences = []
        for row in c.execute(
            "SELECT * FROM food_occurrences WHERE is_current=1 AND source_date BETWEEN ? AND ? ORDER BY id",
            (start.isoformat(), today.isoformat()),
        ).fetchall():
            occurrence = dict(row)
            occurrence["nutrients"] = _nutrients(c, row["id"])
            occurrences.append(occurrence)
        anomalies = quality = 0
        codes = Counter()
        affected_foods = set()
        for occurrence in occurrences:
            findings = detect_anomalies(
                occurrence["nutrients"], amount=occurrence["amount"], unit=occurrence["unit"]
            )
            if (
                occurrence.get("source_food_id") == "f96a4d4da2c2c98d064aac70c45fd3ee"
                and occurrence["nutrients"].get("sodium_mg") == 300
            ):
                findings.append(
                    {"code": "user_confirmed_serving_sodium_contradiction", "severity": "error",
                     "nutrients": ["sodium_mg"], "confirmed_actual_slices": 4,
                     "resolved_sodium_mg": 600}
                )
            formulation = _latest_formulation(c, occurrence.get("source_food_id")) if occurrence.get("source_food_id") else None
            if formulation:
                resolved = library_values_for_occurrence(c, occurrence)
                source_cal = occurrence["nutrients"].get("calories")
                known_cal = resolved.get("calories", {}).get("value")
                if source_cal and known_cal and abs(source_cal - known_cal) > max(30, known_cal * .2):
                    findings.append({"code": "known_food_per_serving_fingerprint_changed",
                                     "severity": "warning", "nutrients": [],
                                     "source_calories": source_cal, "expected_calories": known_cal})
            for finding in findings:
                a, q = _write_finding(c, audit_id, occurrence, finding)
                anomalies += a
                quality += q
                codes[finding["code"]] += 1
                affected_foods.add(occurrence.get("source_food_id") or occurrence["id"])
        resolved = refresh_occurrence_resolutions(c)
        summary = {"occurrences_audited": len(occurrences), "new_anomaly_findings": anomalies,
                   "invalid_source_nutrient_fields": quality, "affected_foods": len(affected_foods),
                   "finding_counts": dict(sorted(codes.items())), "occurrence_resolutions_added": resolved}
        c.execute(
            "UPDATE nutrition_audit_runs SET status='complete',summary_json=?,completed_at=? WHERE id=?",
            (_json(summary), _now(), audit_id),
        )
        return {"status": "complete", "cadence": cadence, "audit_run_id": audit_id, **summary}


def run_due_audits(data_dir: Path, *, today: date | None = None) -> dict:
    today = today or date.today()  # noqa: DTZ011
    return {
        cadence: run_audit(data_dir, cadence, end=today)
        for cadence in ("daily", "weekly", "monthly")
    }


def reconcile_review_state(c) -> None:
    resolved_ids = {row[0] for row in c.execute("SELECT source_food_id FROM food_source_identities")}
    if not resolved_ids:
        return
    for food_id in resolved_ids:
        flag = c.execute(
            "SELECT status FROM source_review_flags WHERE source_food_id=? ORDER BY id DESC LIMIT 1",
            (food_id,),
        ).fetchone()
        if flag and flag[0] != "cleared":
            c.execute(
                "INSERT INTO source_review_flags(source_food_id,status,note,created_at) VALUES (?,'cleared',?,?)",
                (food_id, "Resolved Food Library now contains a durable serving and nutrient resolution.", _now()),
            )
        for proposal in c.execute(
            "SELECT id FROM research_proposals WHERE source_food_id=?", (food_id,)
        ).fetchall():
            latest = c.execute(
                "SELECT action FROM proposal_reviews WHERE proposal_id=? ORDER BY id DESC LIMIT 1",
                (proposal[0],),
            ).fetchone()
            if latest and latest[0] in {"imported", "needs_review", "ready", "partially_approved", "deferred"}:
                c.execute(
                    "INSERT INTO proposal_reviews(proposal_id,action,note,created_at,actor) VALUES (?,'resolved_library',?,?,'policy')",
                    (proposal[0], "Superseded by the durable Resolved Food Library; prior evidence remains preserved.", _now()),
                )


def library_status(data_dir: Path) -> dict:
    with NutritionRepository(data_dir) as repo, repo.connection as c:
        counts = {
            "canonical_foods": c.execute("SELECT COUNT(*) FROM canonical_foods").fetchone()[0],
            "source_identities": c.execute("SELECT COUNT(*) FROM food_source_identities").fetchone()[0],
            "formulations": c.execute("SELECT COUNT(*) FROM food_formulations").fetchone()[0],
            "active_formulations": c.execute(
                "SELECT COUNT(*) FROM food_formulations f JOIN "
                "(SELECT canonical_food_id,MAX(version) version FROM food_formulations GROUP BY canonical_food_id) x "
                "ON x.canonical_food_id=f.canonical_food_id AND x.version=f.version"
            ).fetchone()[0],
            "nutrient_profiles": c.execute("SELECT COUNT(*) FROM formulation_nutrients").fetchone()[0],
            "user_confirmed_mappings": c.execute(
                "SELECT COUNT(*) FROM formulation_servings s JOIN food_formulations f ON f.id=s.formulation_id "
                "JOIN (SELECT canonical_food_id,MAX(version) version FROM food_formulations GROUP BY canonical_food_id) x "
                "ON x.canonical_food_id=f.canonical_food_id AND x.version=f.version "
                "WHERE s.mapping_type='user_confirmed'"
            ).fetchone()[0],
            "active_anomalies": c.execute(
                "SELECT COUNT(DISTINCT occurrence_id||':'||code) FROM nutrition_anomaly_findings"
            ).fetchone()[0],
            "invalid_source_nutrient_fields": c.execute(
                "SELECT COUNT(DISTINCT occurrence_id||':'||nutrient||':'||source_value) FROM source_nutrient_quality_findings WHERE status='invalid'"
            ).fetchone()[0],
        }
        provenance = dict(c.execute(
            "SELECT n.provenance,COUNT(DISTINCT n.formulation_id) FROM formulation_nutrients n "
            "JOIN food_formulations f ON f.id=n.formulation_id JOIN "
            "(SELECT canonical_food_id,MAX(version) version FROM food_formulations GROUP BY canonical_food_id) x "
            "ON x.canonical_food_id=f.canonical_food_id AND x.version=f.version GROUP BY n.provenance"
        ).fetchall())
        confidence = dict(c.execute(
            "SELECT n.confidence,COUNT(*) FROM formulation_nutrients n JOIN food_formulations f "
            "ON f.id=n.formulation_id JOIN "
            "(SELECT canonical_food_id,MAX(version) version FROM food_formulations GROUP BY canonical_food_id) x "
            "ON x.canonical_food_id=f.canonical_food_id AND x.version=f.version GROUP BY n.confidence"
        ).fetchall())
        audits = {}
        for cadence in ("daily", "weekly", "monthly", "full_history"):
            row = c.execute(
                "SELECT completed_at,summary_json FROM nutrition_audit_runs WHERE cadence=? AND status='complete' ORDER BY id DESC LIMIT 1",
                (cadence,),
            ).fetchone()
            audits[cadence] = ({"completed_at": row[0], "summary": json.loads(row[1])} if row else None)
        counts["historical_anomaly_findings"] = counts["active_anomalies"]
        return counts | {"foods_by_nutrient_provenance": provenance,
                         "nutrient_fields_by_confidence": confidence, "last_audits": audits}
