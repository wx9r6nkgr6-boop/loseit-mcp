"""Period/status and evidence regressions for the new nutrition views."""

import fcntl
import plistlib
from datetime import date, timedelta

import pytest

from loseit_mcp.food_patterns import (
    _obvious,
    added_sugar_period,
    build_usda_pattern_provider,
    classify_obvious,
    read_pattern_period,
    record_added_sugar,
    record_pattern_evidence,
    run_added_sugar_pilot,
    weekly_audit,
)
from loseit_mcp.metric_engine import (
    period_bounds,
    prominent_keys,
    read_metric_dashboard,
    status_for,
)
from loseit_mcp.repository import NutritionRepository
from loseit_mcp.resolved import populate_library
from loseit_mcp.weekly_scheduler import generate_configuration
from loseit_mcp.weekly_scheduler import run as run_weekly
from tests.test_research_queue import item


@pytest.mark.parametrize(("day", "expected"), [
    (date(2026, 9, 21), ("2026-09-21", "2026-09-21", "2026-09-14", "2026-09-14")),
    (date(2026, 9, 27), ("2026-09-21", "2026-09-27", "2026-09-14", "2026-09-20")),
    (date(2024, 3, 1), ("2024-02-01", "2024-03-01", "2024-01-02", "2024-01-31")),
])
def test_period_boundaries(day, expected):
    period = "rolling_30" if day.year == 2024 else "current_week"
    assert tuple(value.isoformat() for value in period_bounds(period, day)) == expected
    assert period_bounds("last_week", date(2026, 9, 27))[:2] == (
        date(2026, 9, 14), date(2026, 9, 20))


@pytest.mark.parametrize(("key", "value", "expected"), [
    ("protein_g", 150, "excellent"), ("protein_g", 180, "excellent"),
    ("protein_g", 130, "on_track"), ("protein_g", 115, "slightly_off_track"),
    ("protein_g", 95, "needs_attention"), ("protein_g", 94.9, "significantly_off_track"),
    ("fiber_g", 28, "excellent"), ("sodium_mg", 2300, "on_track"),
    ("sodium_mg", 2600, "needs_attention"),
    ("sugar_g", 80, "informational"), ("total_fat_g", 40, "informational"),
    ("calories", 0, "on_track"), ("weight", 1, "informational"),
])
def test_metric_aware_statuses(key, value, expected):
    assert status_for(key, value) == expected
    assert status_for(key, value, reportable=False) == "insufficient_data"


def test_prominent_metrics_are_key_based_and_support_more_than_three():
    definitions = [{"key": "calories", "prominent_order": 4},
                   {"key": "fiber_g", "prominent_order": 2},
                   {"key": "weight", "prominent_order": None},
                   {"key": "protein_g", "prominent_order": 1},
                   {"key": "sugar_g", "prominent_order": 3}]
    assert prominent_keys(definitions) == ["protein_g", "fiber_g", "sugar_g", "calories"]


def _seed(tmp_path, day, entries):
    with NutritionRepository(tmp_path) as repo:
        repo.ingest_day({"date": day.isoformat(), "entries": entries}, retrieved_at=day.isoformat())


def _nutrients(protein=100, sugar=25, calories=1000):
    return {"calories": calories, "protein_g": protein, "fiber_g": 28,
            "sugar_g": sugar, "saturated_fat_g": 10, "sodium_mg": 2000,
            "total_fat_g": 30, "carb_g": 100, "cholesterol_mg": 100}


def test_partial_today_missing_days_previous_and_calorie_allowance(tmp_path):
    monday = date(2026, 9, 21)
    prior = monday - timedelta(days=7)
    _seed(tmp_path, prior, [item(food_id="older", nutrients=_nutrients(120), entry_id="older")])
    _seed(tmp_path, monday, [item(food_id="current", nutrients=_nutrients(140), entry_id="current")])
    _seed(tmp_path, monday + timedelta(days=4), [item(food_id="today", nutrients=_nutrients(20), entry_id="today")])
    result = read_metric_dashboard(tmp_path, "current_week", today=monday + timedelta(days=4))
    current = result["current"]
    assert current["eligible_days"] == 1
    assert current["current_day_partial"] is True
    assert current["nutrients"]["protein_g"]["value"] == 140
    assert current["nutrients"]["protein_g"]["previous_comparable_value"] == 120
    assert current["nutrients"]["sugar_g"]["status"] == "informational"
    assert current["nutrients"]["added_sugar_g"]["value"] is None
    assert current["nutrients"]["calories"]["status"] == "informational"
    assert current["nutrients"]["protein_g"]["source_occurrences"] == 1
    assert "2026-09-22" in current["missing_days"]


def test_variable_daily_allowance_uses_matched_sum(tmp_path):
    monday = date(2026, 9, 21)
    for index in range(2):
        day = monday + timedelta(days=index)
        _seed(tmp_path, day, [item(food_id=f"x{index}", entry_id=f"x{index}",
                                  nutrients=_nutrients(calories=1000))])
    with NutritionRepository(tmp_path) as repo, repo.connection:
        for index, allowance in enumerate((900, 1100)):
            repo.connection.execute(
                """INSERT INTO daily_allowance_observations
                   (source_date,final_daily_allowance,provenance,source_reference,captured_at,content_sha256)
                   VALUES (?,?,?,?,?,?)""",
                ((monday + timedelta(days=index)).isoformat(), allowance,
                 "loseit_validated_final", "test", "2026-09-23", f"allowance-{index}"),
            )
    report = read_metric_dashboard(tmp_path, "current_week", today=date(2026, 9, 24))
    calories = report["current"]["nutrients"]["calories"]
    assert calories["budget_total"] == 2000
    assert calories["budget_deviation_pct"] == 0
    assert calories["budget_days"] == 2


def test_unvalidated_allowance_is_ignored(tmp_path):
    day = date(2026, 9, 21)
    _seed(tmp_path, day, [item(food_id="one", nutrients=_nutrients(), entry_id="one")])
    with NutritionRepository(tmp_path) as repo, repo.connection:
        repo.connection.execute(
            """INSERT INTO daily_allowance_observations
               (source_date,final_daily_allowance,provenance,source_reference,captured_at,content_sha256)
               VALUES (?,?,?,?,?,?)""",
            (day.isoformat(), 1000, "unverified_guess", "test", "2026-09-23", "guess"),
        )
    calories = read_metric_dashboard(tmp_path, "current_week", today=date(2026, 9, 24))["current"]["nutrients"]["calories"]
    assert calories["status"] == "informational"
    assert calories["budget_total"] is None


def test_weight_trailing_week_crosses_monday_boundary(tmp_path):
    _seed(tmp_path, date(2026, 9, 21), [])
    with NutritionRepository(tmp_path) as repo, repo.connection:
        for day, weight in (("2026-09-19", 200), ("2026-09-20", 199),
                            ("2026-09-22", 198), ("2026-09-24", 197)):
            repo.connection.execute(
                "INSERT INTO weight_observations(source,source_date,weight,unit,retrieved_at) VALUES (?,?,?,?,?)",
                ("fixture", day, weight, "lb", day),
            )
    weight = read_metric_dashboard(tmp_path, "current_week", today=date(2026, 9, 25))["current"]["nutrients"]["weight"]
    assert weight["observations"] == 4
    assert weight["value"] == 198.5
    assert weight["unit"] == "lb"


def test_food_identity_rules_are_conservative():
    assert {row[0] for row in _obvious("Fish, Salmon, New Orleans")} == {"seafood", "fatty_fish"}
    assert _obvious("Blackened Shrimp Tacos")[0][1] == "presence_known_quantity_unknown"
    assert _obvious("Broccoli Florets Frozen")[0][0] == "vegetable"
    assert _obvious("Banana Raw")[0][0] == "fruit"
    assert _obvious("Beans, Pinto")[0][0] == "legume"
    assert _obvious("100% Whole Wheat Bagel")[0][0] == "whole_grain"
    assert _obvious("Almonds Raw")[0][0] == "nut_seed"
    assert _obvious("Pizza with tomato sauce") == []
    assert _obvious("Chicken with parsley garnish") == []


def test_user_confirmed_pattern_override_is_append_only(tmp_path):
    day = date(2026, 9, 21)
    _seed(tmp_path, day, [item(food_id="broccoli", name="Broccoli Florets Frozen",
                               brand="", entry_id="broccoli", nutrients=_nutrients())])
    populate_library(tmp_path)
    with NutritionRepository(tmp_path) as repo, repo.connection:
        c = repo.connection
        classify_obvious(c)
        initial = c.execute("SELECT id,canonical_food_id,formulation_id FROM food_pattern_evidence").fetchone()
        assert read_pattern_period(c, day, day)["vegetable"]["meaningful_occurrences"] == 1
        assert record_pattern_evidence(c, canonical_food_id=initial[1],
                                       formulation_id=initial[2], pattern_key="vegetable",
                                       presence_class="none_verified", provenance="user_confirmed",
                                       confidence="high", evidence_basis="Fixture user correction",
                                       user_confirmed=True, supersedes_id=initial[0])
        assert c.execute("SELECT COUNT(*) FROM food_pattern_evidence").fetchone()[0] == 2
        assert read_pattern_period(c, day, day)["vegetable"]["meaningful_occurrences"] == 0


def test_pattern_and_added_sugar_evidence_idempotence_and_provider_failure(tmp_path):
    day = date(2026, 9, 21)
    _seed(tmp_path, day, [item(food_id="broccoli", name="Broccoli Florets Frozen", brand="",
                               entry_id="broccoli", nutrients=_nutrients()),
                          item(food_id="mixed", name="Pizza with tomato sauce",
                               entry_id="mixed", nutrients=_nutrients())])
    populate_library(tmp_path)
    with NutritionRepository(tmp_path) as repo, repo.connection:
        c = repo.connection
        assert classify_obvious(c)["classifications_created"] == 1
        assert classify_obvious(c)["classifications_created"] == 0
        patterns = read_pattern_period(c, day, day)
        assert patterns["vegetable"]["meaningful_occurrences"] == 1
        assert patterns["vegetable"]["verified_quantity"] is None
        assert patterns["plant_variety"]["distinct_plants"] == 1
        assert patterns["vegetable"]["unknown_occurrences"] == 1
        pilot = run_added_sugar_pilot(c, start=day, end=day)
        assert pilot["evidence_created"] == 1
        assert run_added_sugar_pilot(c, start=day, end=day)["evidence_created"] == 0
        assert c.execute("SELECT COUNT(*) FROM added_sugar_pilot_runs").fetchone()[0] == 1
        # Fixture serving is generic "serving", so zero evidence is stored but not
        # projected unless the recorded formulation portion is safely scalable.
        added = added_sugar_period(c, day, day)
        assert added["known_total_g"] in (0, None)
        before = c.execute("SELECT COUNT(*) FROM raw_diary_snapshots").fetchone()[0]
    first = weekly_audit(tmp_path, today=day, research_provider=lambda _: (_ for _ in ()).throw(RuntimeError("offline")))
    second = weekly_audit(tmp_path, today=day, research_provider=lambda _: (_ for _ in ()).throw(RuntimeError("offline")))
    assert first["provider_failures"] == second["provider_failures"] == 1
    with NutritionRepository(tmp_path) as repo:
        c = repo.connection
        assert c.execute("SELECT COUNT(*) FROM raw_diary_snapshots").fetchone()[0] == before
        assert c.execute("SELECT COUNT(*) FROM food_pattern_evidence").fetchone()[0] == 1


def test_weekly_job_is_separate_secret_free_and_weekend(tmp_path):
    config = generate_configuration(tmp_path, python="/safe/python")
    payload = plistlib.dumps(config).decode()
    assert config["StartCalendarInterval"] == {"Weekday": 7, "Hour": 11, "Minute": 30}
    assert "liauth" not in payload and "api_key" not in payload


def test_weekly_job_respects_daily_update_lock(tmp_path):
    _seed(tmp_path, date(2026, 9, 21), [])
    with (tmp_path / "backfill.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert run_weekly(tmp_path)["status"] == "busy"


def test_added_sugar_exact_label_scales_and_never_uses_total_sugar(tmp_path):
    day = date(2026, 9, 21)
    _seed(tmp_path, day, [item(food_id="cookie", name="Exact Cookie", brand="Brand",
                               amount=2, entry_id="cookie", nutrients=_nutrients(sugar=42, calories=400))])
    populate_library(tmp_path)
    with NutritionRepository(tmp_path) as repo, repo.connection:
        c = repo.connection
        food = c.execute("SELECT canonical_food_id FROM food_source_identities WHERE source_food_id='cookie'").fetchone()[0]
        formulation = c.execute("""SELECT f.id,s.canonical_amount,s.canonical_unit
                                 FROM food_formulations f JOIN formulation_servings s ON s.formulation_id=f.id
                                 WHERE f.canonical_food_id=? LIMIT 1""", (food,)).fetchone()
        assert added_sugar_period(c, day, day)["known_total_g"] is None
        with pytest.raises(ValueError, match="validity date"):
            record_added_sugar(c, canonical_food_id=food, formulation_id=formulation[0],
                               value_g=5, basis_amount=formulation[1], basis_unit=formulation[2],
                               provenance="current_product_label", confidence="high",
                               evidence_basis="Fixture exact label")
        record_added_sugar(c, canonical_food_id=food, formulation_id=formulation[0],
                           value_g=5, basis_amount=formulation[1], basis_unit=formulation[2],
                           provenance="current_product_label", confidence="high",
                           evidence_basis="Fixture exact label", valid_from=day.isoformat())
        result = added_sugar_period(c, day, day)
        assert result["known_total_g"] == 10
        assert result["known_total_g"] != 42
        assert result["reportable"] is True


def test_current_manufacturer_label_does_not_backfill_historical_occurrence(tmp_path):
    day = date(2026, 9, 21)
    _seed(tmp_path, day, [item(food_id="broccoli", name="Broccoli Florets Frozen",
                               brand="Great Value", entry_id="broccoli",
                               nutrients=_nutrients())])
    populate_library(tmp_path)
    with NutritionRepository(tmp_path) as repo, repo.connection:
        c = repo.connection
        pilot = run_added_sugar_pilot(c, start=day, end=day, research_date=date(2026, 9, 25))
        assert pilot["evidence_created"] == 1
        assert pilot["reviewed"][0]["decision"] == "current_label_zero_not_historical"
        assert added_sugar_period(c, day, day)["known_occurrences"] == 0
        assert run_added_sugar_pilot(c, start=day, end=day,
                                     research_date=date(2026, 9, 26))["evidence_created"] == 0


def test_usda_pattern_research_requires_exact_generic_identity(monkeypatch):
    from loseit_mcp import credentials, nutrition_provider

    monkeypatch.setattr(credentials, "load_usda_api_key", lambda: "fixture-key")
    monkeypatch.setattr(nutrition_provider.UsdaFoodDataCentralWorker, "_request",
                        lambda self, *_args, **_kwargs: {"foods": [
                            {"description": "Cauliflower, Riced", "dataType": "SR Legacy",
                             "foodCategory": "Vegetables and Vegetable Products", "fdcId": 123}]})
    provider = build_usda_pattern_provider()
    assert provider is not None
    result = provider({"name": "Cauliflower, Riced", "brand": ""})
    assert result[0]["pattern_key"] == "vegetable"
    assert result[0]["provenance"] == "USDA_exact_generic_category"
    assert provider({"name": "Cauliflower, Riced", "brand": "Some Brand"}) == []
    assert provider({"name": "Pizza with tomato sauce", "brand": ""}) == []
