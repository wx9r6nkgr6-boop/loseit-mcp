from datetime import date

import pytest

from loseit_mcp.analytics import read_analytics
from loseit_mcp.repository import NutritionRepository
from loseit_mcp.research_queue import read_research_queue
from loseit_mcp.resolved import (
    combined_values_for_occurrence,
    detect_anomalies,
    library_status,
    library_values_for_occurrence,
    populate_library,
    run_audit,
)
from tests.test_research_queue import item


def seed(path, entries, day="2026-09-01"):
    with NutritionRepository(path) as repo:
        repo.ingest_day({"date": day, "entries": entries}, retrieved_at=day)


@pytest.mark.parametrize(
    ("nutrients", "expected"),
    [
        ({"calories": 320, "total_fat_g": 320}, "impossible_single_macro_energy"),
        (
            {"calories": 640, "protein_g": 53.6, "carb_g": 72.8, "total_fat_g": 220},
            "impossible_single_macro_energy",
        ),
        (
            {"calories": 90, "protein_g": 2, "carb_g": 40, "total_fat_g": 14},
            "calorie_macro_contradiction",
        ),
    ],
)
def test_required_corrupt_source_anomalies(nutrients, expected):
    assert expected in {finding["code"] for finding in detect_anomalies(nutrients)}


def test_normal_label_rounding_not_flagged():
    findings = detect_anomalies(
        {"calories": 160, "protein_g": 2, "carb_g": 25, "total_fat_g": 7}
    )
    assert not [finding for finding in findings if finding["severity"] == "error"]


def test_canonical_library_scaling_fractional_and_quantity_reuse(tmp_path):
    food_id = "4f53d67a020b659a73482c6a699e2e1b"
    seed(
        tmp_path,
        [
            item(food_id=food_id, amount=.5, nutrients={"calories": 80}, entry_id="half"),
            item(food_id=food_id, amount=5, nutrients={"calories": 800}, entry_id="five"),
        ],
    )
    result = populate_library(tmp_path)
    assert result["canonical_foods_created"] == 1
    with NutritionRepository(tmp_path) as repo:
        rows = repo.connection.execute("SELECT * FROM food_occurrences ORDER BY id").fetchall()
        half = library_values_for_occurrence(repo.connection, dict(rows[0]))
        five = library_values_for_occurrence(repo.connection, dict(rows[1]))
        assert half["carb_g"]["value"] == 13
        assert five["carb_g"]["value"] == 130
        assert half["carb_g"]["provenance"] == "researched_exact"
    assert read_research_queue(tmp_path)["queue_count"] == 0


def test_unknown_unit_conversion_refused(tmp_path):
    seed(tmp_path, [item(food_id="generic", amount=1, nutrients={"calories": 100})])
    populate_library(tmp_path)
    with NutritionRepository(tmp_path) as repo:
        row = dict(repo.connection.execute("SELECT * FROM food_occurrences").fetchone())
        row["unit"] = "cup"
        assert library_values_for_occurrence(repo.connection, row) == {}


def test_explicit_two_ids_share_one_canonical_food_and_preserve_ids(tmp_path):
    seed(
        tmp_path,
        [
            item(food_id="8134c5320a6c1f829941230cefc2aa44", name="Meatloaf & Gravy", brand="Banquet", entry_id="a"),
            item(food_id="b0f04281941eab915044a70b51692c24", name="Meatloaf & Gravy", brand="Banquet", entry_id="b"),
        ],
    )
    populate_library(tmp_path)
    with NutritionRepository(tmp_path) as repo:
        rows = repo.connection.execute(
            "SELECT source_food_id,canonical_food_id FROM food_source_identities ORDER BY source_food_id"
        ).fetchall()
        assert len(rows) == 2
        assert len({row["canonical_food_id"] for row in rows}) == 1
        assert {row["source_food_id"] for row in rows} == {
            "8134c5320a6c1f829941230cefc2aa44",
            "b0f04281941eab915044a70b51692c24",
        }


def test_invalid_source_preserved_but_combined_analytics_uses_resolution(tmp_path):
    food_id = "1655fc0418878d9d8f4c9fb40b1cb35c"
    seed(
        tmp_path,
        [item(food_id=food_id, nutrients={"calories": 320, "total_fat_g": 320})],
    )
    populate_library(tmp_path)
    audit = run_audit(
        tmp_path, "full_history", start=date(2026, 1, 1), end=date(2026, 9, 1), force=True
    )
    assert audit["invalid_source_nutrient_fields"] == 1
    with NutritionRepository(tmp_path) as repo:
        occurrence = dict(repo.connection.execute("SELECT * FROM food_occurrences").fetchone())
        source = repo.connection.execute(
            "SELECT value FROM nutrient_observations WHERE nutrient='total_fat_g'"
        ).fetchone()[0]
        combined = combined_values_for_occurrence(repo.connection, occurrence)
        assert source == 320
        assert combined["total_fat_g"]["value"] == 16
        assert combined["total_fat_g"]["supersedes_invalid_source_value"] == 320
    report = read_analytics(
        tmp_path, date(2026, 9, 1), date(2026, 9, 1), compare=False
    )
    assert report["period_averages"]["total_fat_g"]["combined_usable"] == 16
    assert report["evidence_quality_coverage"]["total_fat_g"]["by_provenance"] == {
        "modeled_estimate": 1
    }


def test_user_confirmed_oreo_and_arnold_mappings_persist(tmp_path):
    oreo = item(
        food_id="49c67296822d7b85b94fd3793a3e8465",
        name="Oreo Frozen Dessert Mini Cones",
        brand="Oreo",
        amount=1,
        nutrients={"calories": 220},
        entry_id="oreo",
    )
    oreo["unit"] = "piece"
    bread = item(
        food_id="f96a4d4da2c2c98d064aac70c45fd3ee",
        name="Bread, Oatnut",
        brand="Arnold",
        amount=2,
        nutrients={"calories": 480, "sodium_mg": 300},
        entry_id="bread",
    )
    bread["unit"] = "slice"
    seed(
        tmp_path,
        [oreo, bread],
    )
    populate_library(tmp_path)
    run_audit(
        tmp_path, "full_history", start=date(2026, 1, 1), end=date(2026, 9, 1), force=True
    )
    with NutritionRepository(tmp_path) as repo:
        rows = {
            row["source_food_id"]: dict(row)
            for row in repo.connection.execute("SELECT * FROM food_occurrences")
        }
        oreo = library_values_for_occurrence(
            repo.connection, rows["49c67296822d7b85b94fd3793a3e8465"]
        )
        bread = library_values_for_occurrence(
            repo.connection, rows["f96a4d4da2c2c98d064aac70c45fd3ee"]
        )
        assert oreo["calories"]["value"] == 220
        assert oreo["cholesterol_mg"]["value"] == 0
        assert bread["protein_g"]["value"] == 16
        assert bread["sodium_mg"]["value"] == 600
        combined_bread = combined_values_for_occurrence(
            repo.connection, rows["f96a4d4da2c2c98d064aac70c45fd3ee"]
        )
        assert combined_bread["sodium_mg"]["value"] == 600
        assert combined_bread["sodium_mg"]["supersedes_invalid_source_value"] == 300
    assert library_status(tmp_path)["user_confirmed_mappings"] == 2


def test_audit_cadence_gating_and_idempotence(tmp_path):
    seed(tmp_path, [item(food_id="x", nutrients={"calories": 100})])
    first = run_audit(tmp_path, "weekly", end=date(2026, 9, 1))
    second = run_audit(tmp_path, "weekly", end=date(2026, 9, 2))
    assert first["status"] == "complete"
    assert second["status"] == "already_completed"
    assert run_audit(tmp_path, "monthly", end=date(2026, 9, 2))["status"] == "complete"
