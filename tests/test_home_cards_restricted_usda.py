"""Privacy, customization, and goal-card contracts."""

import json
from datetime import date

import pytest

from loseit_mcp.food_patterns import added_sugar_period, weekly_audit
from loseit_mcp.home_cards import (
    attention_keys,
    card_model,
    change_preference,
    load_preferences,
    normalize_preferences,
    save_preferences,
)
from loseit_mcp.metric_engine import METRICS
from loseit_mcp.nutrition_provider import USDA_API_URL, UsdaFoodDataCentralWorker
from loseit_mcp.repository import NutritionRepository
from loseit_mcp.resolved import populate_library
from loseit_mcp.restricted_usda import ProductIdentity, RestrictedUsdaClient
from tests.test_research_queue import item

DEFINITIONS = [definition.__dict__ for definition in METRICS]
BY_KEY = {definition["key"]: definition for definition in DEFINITIONS}


def result(value, status="on_track", quality="Verified", reportable=True, **extra):
    return {"value": value, "status": status, "status_label": status.replace("_", " ").title(),
            "quality": quality, "reportable": reportable, **extra}


def test_pins_reorder_hide_restore_and_corrupt_fallback(tmp_path):
    defaults = load_preferences(tmp_path, DEFINITIONS)
    assert defaults["pinned"] == ["protein_g", "fiber_g", "sugar_g"]
    fourth = change_preference(defaults, "pin", "sodium_mg", DEFINITIONS)
    fifth = change_preference(fourth, "pin", "saturated_fat_g", DEFINITIONS)
    assert len(fifth["pinned"]) == 5
    moved = change_preference(fifth, "up", "sodium_mg", DEFINITIONS)
    assert moved["pinned"].index("sodium_mg") == 2
    unpinned = change_preference(moved, "unpin", "sodium_mg", DEFINITIONS)
    assert "sodium_mg" not in unpinned["pinned"]
    hidden = change_preference(unpinned, "hide", "protein_g", DEFINITIONS)
    assert "protein_g" in hidden["hidden"] and "protein_g" not in hidden["pinned"]
    restored = change_preference(hidden, "restore", "protein_g", DEFINITIONS)
    assert "protein_g" not in restored["hidden"]
    save_preferences(tmp_path, restored, DEFINITIONS)
    assert load_preferences(tmp_path, DEFINITIONS) == restored
    assert (tmp_path / "dashboard_preferences.json").stat().st_mode & 0o077 == 0
    (tmp_path / "dashboard_preferences.json").write_text("{")
    assert load_preferences(tmp_path, DEFINITIONS) == defaults
    assert normalize_preferences({"version": 1, "pinned": ["retired", "protein_g"],
                                  "hidden": ["retired", "sodium_mg"]}, DEFINITIONS) == {
                                      "version": 1, "pinned": ["protein_g"], "hidden": ["sodium_mg"]}
    introduced = DEFINITIONS + [{"key": "new_metric", "name": "New"}]
    assert "new_metric" not in normalize_preferences(restored, introduced)["hidden"]


def test_card_progress_semantics_and_no_fake_sugar_goal():
    protein = card_model(BY_KEY["protein_g"], result(111))
    assert protein["progress_kind"] == "adequacy"
    assert protein["goal_text"] == "Goal: 150 g/day · On Track from 130 g/day"
    assert protein["progress_fraction"] == pytest.approx(.74)
    assert card_model(BY_KEY["protein_g"], result(190))["progress_fraction"] == 1
    fiber = card_model(BY_KEY["fiber_g"], result(22.8))
    assert "28 g/day" in fiber["goal_text"] and fiber["progress_fraction"] == pytest.approx(22.8 / 28)
    sodium = card_model(BY_KEY["sodium_mg"], result(3227, status="needs_attention"))
    assert sodium["progress_kind"] == "limit_over" and "over daily limit" in sodium["progress_text"]
    assert "2,300 mg/day" in sodium["goal_text"]
    saturated = card_model(BY_KEY["saturated_fat_g"], result(13.6))
    assert saturated["goal_text"] == "Goal: < 10% kcal"
    assert saturated["progress_text"] == "3.6 percentage points over limit"
    for key in ("sugar_g", "total_fat_g", "carb_g", "cholesterol_mg", "weight", "calories"):
        model = card_model(BY_KEY[key], result(80, status="informational"))
        assert model["goal_text"] is None and model["progress_kind"] is None
    assert "unit unconfirmed" in card_model(BY_KEY["weight"], result(196.4))["value_text"]
    unavailable = card_model(BY_KEY["added_sugar_g"], result(None, reportable=False))
    assert unavailable["progress_kind"] is None
    hypothetical = {"key": "custom_range", "name": "Range", "unit": "units",
                    "semantic_type": "target_range", "minimum": 20, "maximum": 30}
    assert card_model(hypothetical, result(25))["progress_kind"] == "range"


def test_attention_surfaces_only_unpinned_reliable_actionable_metrics():
    values = {key: result(1, status="informational") for key in BY_KEY}
    values["sodium_mg"] = result(3300, status="significantly_off_track")
    values["saturated_fat_g"] = result(15, status="needs_attention")
    values["sugar_g"] = result(100, status="informational")
    values["added_sugar_g"] = result(None, status="needs_attention", quality="Incomplete", reportable=False)
    view = {"definitions": DEFINITIONS, "current": {"eligible_days": 3, "nutrients": values}}
    prefs = normalize_preferences(None, DEFINITIONS)
    assert attention_keys(view, prefs) == ["sodium_mg", "saturated_fat_g"]
    prefs = change_preference(prefs, "pin", "sodium_mg", DEFINITIONS)
    assert attention_keys(view, prefs) == ["saturated_fat_g"]
    prefs = change_preference(prefs, "hide", "saturated_fat_g", DEFINITIONS)
    assert attention_keys(view, prefs) == []
    view["current"]["eligible_days"] = 1
    assert attention_keys(view, normalize_preferences(None, DEFINITIONS)) == []


class FakeWorker:
    def __init__(self, search, detail):
        self.calls = []
        self.search = search
        self.detail = detail

    def _request(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs))
        return self.search if path == "/foods/search" else self.detail

    @staticmethod
    def _identity_matches(food, name, brand):
        from loseit_mcp.nutrition_provider import UsdaFoodDataCentralWorker

        return UsdaFoodDataCentralWorker._identity_matches(food, name, brand)


def test_restricted_request_contains_only_brand_and_name():
    food = {"fdcId": 123, "description": "Exact Cookie", "brandOwner": "Test Brand",
            "dataType": "Branded", "foodNutrients": [{"nutrient": {"name": "Sugars, added", "unitName": "G"},
                                                      "amount": 20}]}
    worker = FakeWorker({"foods": [food]}, food)
    client = RestrictedUsdaClient(worker)
    finding = client.review(ProductIdentity("Exact Cookie", "Test Brand"))
    assert finding["added_sugar_per_100g"] == 20
    assert worker.calls == [
        ("POST", "/foods/search", {"json": {"query": "test brand exact cookie",
                                             "dataType": ["Branded"], "pageSize": 20}}),
        ("GET", "/food/123", {}),
    ]
    serialized = json.dumps(worker.calls).casefold()
    for forbidden in ("occurrences", "2026-09", "breakfast", "account", "user_id",
                      "logged_quantity", "diary_context", "calories"):
        assert forbidden not in serialized
    for field, value in (("occurrences", 14), ("source_date", "2026-09-26"),
                         ("meal_name", "Breakfast"), ("logged_quantity", 3),
                         ("user_id", "private"), ("diary_context", "with other foods")):
        with pytest.raises(TypeError):
            ProductIdentity("Exact Cookie", "Test Brand", **{field: value})
    with pytest.raises(ValueError, match="private context"):
        ProductIdentity("Exact Cookie 2026-09-26", "Test Brand")
    with pytest.raises(ValueError, match="private context"):
        ProductIdentity("Exact Cookie", "user@example.com")


def test_actual_http_request_has_no_private_context():
    food = {"fdcId": 123, "description": "Exact Cookie", "brandOwner": "Test Brand",
            "dataType": "Branded", "foodNutrients": []}
    class Response:
        def __init__(self, payload):
            self.payload = payload

        def raise_for_status(self):
            pass

        def json(self):
            return self.payload

    class Transport:
        def __init__(self):
            self.calls = []

        def request(self, method, url, **kwargs):
            self.calls.append((method, url, kwargs))
            return Response({"foods": [food]} if url.endswith("/foods/search") else food)

    transport = Transport()
    worker = UsdaFoodDataCentralWorker("fixture-key", client=transport)
    finding = RestrictedUsdaClient(worker).review(ProductIdentity("Exact Cookie", "Test Brand"))
    assert finding["outcome"] == "exact_identity"
    assert transport.calls == [
        ("POST", USDA_API_URL + "/foods/search", {"params": {"api_key": "fixture-key"},
                                                 "json": {"query": "test brand exact cookie",
                                                          "dataType": ["Branded"], "pageSize": 20}}),
        ("GET", USDA_API_URL + "/food/123", {"params": {"api_key": "fixture-key"}}),
    ]
    for term in ("occurrences", "2026-09", "meal", "quantity", "account", "diary", "calories"):
        assert term not in json.dumps(transport.calls).casefold()


def test_usda_near_match_or_total_sugar_is_not_added_sugar():
    almost = {"fdcId": 123, "description": "Similar Cookie", "brandOwner": "Test Brand", "dataType": "Branded"}
    worker = FakeWorker({"foods": [almost]}, almost)
    assert RestrictedUsdaClient(worker).review(ProductIdentity("Exact Cookie", "Test Brand"))["outcome"] == "no_exact_identity"
    exact = almost | {"description": "Exact Cookie", "foodNutrients": [
        {"nutrient": {"name": "Sugars, total", "unitName": "G"}, "amount": 20}]}
    worker = FakeWorker({"foods": [exact]}, exact)
    assert RestrictedUsdaClient(worker).review(ProductIdentity("Exact Cookie", "Test Brand"))["added_sugar_per_100g"] is None


def test_usda_search_normalizes_punctuation_without_adding_context():
    worker = FakeWorker({"foods": []}, {})
    finding = RestrictedUsdaClient(worker).review(
        ProductIdentity("Steak, Salisbury, w/ Brown Gravy", "Banquet Family Entrees"))
    assert finding["outcome"] == "no_exact_identity"
    assert worker.calls[0][2]["json"]["query"] == (
        "banquet family entrees steak salisbury w brown gravy")


def test_usda_generic_category_does_not_quantify_mixed_dish():
    food = {"fdcId": 321, "description": "Tomato Sauce", "dataType": "SR Legacy",
            "foodCategory": "Vegetables and Vegetable Products", "foodNutrients": []}
    finding = RestrictedUsdaClient(FakeWorker({"foods": [food]}, food)).review(
        ProductIdentity("Tomato Sauce"))
    assert finding["outcome"] == "exact_identity"
    assert finding["patterns"] == []


def test_weekly_usda_batch_is_private_idempotent_and_preserves_raw(tmp_path):
    day = date(2026, 9, 26)
    entry = item(food_id="cookie", name="Exact Cookie", brand="Test Brand", entry_id="e1",
                 nutrients={"calories": 100, "protein_g": 2, "sugar_g": 9})
    entry["unit"] = "g"
    entry["amount"] = 25
    entry["servings"] = 25
    with NutritionRepository(tmp_path) as repo:
        repo.ingest_day({"date": day.isoformat(), "entries": [entry]}, retrieved_at=day.isoformat())
    populate_library(tmp_path)
    with NutritionRepository(tmp_path) as repo:
        before = repo.connection.execute("SELECT * FROM raw_diary_snapshots").fetchall()
    class Reviewer:
        def __init__(self):
            self.calls = []

        def review(self, identity):
            self.calls.append(identity)
            return {"outcome": "exact_identity", "fdc_id": 123,
                    "added_sugar_per_100g": 20, "patterns": []}
    reviewer = Reviewer()
    first = weekly_audit(tmp_path, today=day, usda_client=reviewer)
    second = weekly_audit(tmp_path, today=day, usda_client=reviewer)
    assert len(reviewer.calls) == 1
    assert reviewer.calls[0] == ProductIdentity("Exact Cookie", "Test Brand")
    assert first["usda_research"]["added_sugar_evidence_created"] == 1
    assert second["usda_research"]["added_sugar_evidence_created"] == 0
    with NutritionRepository(tmp_path) as repo:
        c = repo.connection
        assert c.execute("SELECT * FROM raw_diary_snapshots").fetchall() == before
        assert c.execute("SELECT COUNT(*) FROM added_sugar_evidence").fetchone()[0] == 1
        assert added_sugar_period(c, day, day)["known_occurrences"] == 1
