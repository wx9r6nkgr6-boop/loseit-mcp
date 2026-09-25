"""Reconnect and USDA provider regressions. No test touches the network or real secrets."""

from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import contextmanager
from dataclasses import replace
from datetime import date

import httpx
import pytest
from lose_it.core._http import LoseItError
from streamlit.testing.v1 import AppTest

from loseit_mcp.config import ConfigError, Settings
from loseit_mcp.credentials import load_usda_api_key, save_usda_api_key, usda_configuration
from loseit_mcp.enrichment_worker import SEEDS
from loseit_mcp.nutrition_provider import (
    ConfiguredResearchWorker,
    ResearchIssue,
    UsdaFoodDataCentralWorker,
)
from loseit_mcp.reconnect import (
    browser_profiles,
    connection_status,
    reconnect_from_browser,
    reconnect_with_token,
)
from loseit_mcp.repository import NutritionRepository
from loseit_mcp.update import latest_research_issues, process_research, run_update
from tests.conftest import make_jwt
from tests.test_dashboard_proposals import APP
from tests.test_research_queue import item, seed
from tests.test_update_workflow import Remote, Worker, factory

TODAY = date(2026, 9, 21)


class ValidatingService:
    calls = 0

    def __init__(self, settings):
        self.settings = settings

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def search_food(self, *args, **kwargs):
        type(self).calls += 1
        return [{"food_id": "fixture"}]


def isolated_settings(tmp_path, token=None):
    return Settings(
        token=token,
        user_id="1",
        user_name="Fixture",
        hours_from_gmt=-4,
        token_file=tmp_path / "liauth",
        session_file=tmp_path / "session.json",
    )


def test_local_connection_status_valid_and_expired_without_network(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "loseit_mcp.reconnect.ReadOnlyLoseItService",
        lambda *_: (_ for _ in ()).throw(AssertionError("network forbidden")),
    )
    assert connection_status(settings=isolated_settings(tmp_path, make_jwt()))["status"] == "connected"
    expired = connection_status(settings=isolated_settings(tmp_path, make_jwt(exp_offset=-1)))
    assert expired["status"] == "reconnect_required"
    assert "token" not in json.dumps(expired).casefold()


def test_successful_reconnect_validates_then_saves_owner_only(tmp_path, valid_token):
    settings = isolated_settings(tmp_path)
    ValidatingService.calls = 0
    result = reconnect_with_token(
        valid_token, settings=settings, service_factory=ValidatingService
    )
    assert result == {"status": "connected", "message": "Lose It reconnected securely."}
    assert ValidatingService.calls == 1
    assert load_usda_api_key(settings.token_file) == valid_token
    assert settings.token_file.stat().st_mode & 0o777 == 0o600
    assert valid_token not in json.dumps(result)


def test_failed_reconnect_never_saves_or_echoes_secret(tmp_path, valid_token, caplog):
    class Rejected(ValidatingService):
        def search_food(self, *args, **kwargs):
            raise LoseItError("rejected " + valid_token)

    caplog.set_level(logging.DEBUG)
    settings = isolated_settings(tmp_path)
    result = reconnect_with_token(valid_token, settings=settings, service_factory=Rejected)
    assert result["status"] == "failed"
    assert not settings.token_file.exists()
    assert valid_token not in json.dumps(result)
    assert valid_token not in caplog.text


def test_browser_reconnect_uses_selected_profile_and_never_returns_token(tmp_path, valid_token):
    seen = []

    def loader(browser, *, profile):
        seen.append((browser, profile))
        return valid_token

    result = reconnect_from_browser(
        "chrome",
        "Profile 2",
        loader=loader,
        settings=isolated_settings(tmp_path),
        service_factory=ValidatingService,
    )
    assert seen == [("chrome", "Profile 2")]
    assert result["status"] == "connected"
    assert valid_token not in json.dumps(result)


def test_profile_listing_strips_cookie_store_path(monkeypatch):
    monkeypatch.setattr(
        "loseit_mcp.reconnect.list_browser_profiles",
        lambda browser: [
            {"directory": "Default", "name": "Personal", "cookie_store": "/private/Cookies"}
        ],
    )
    assert browser_profiles("chrome") == [{"directory": "Default", "name": "Personal"}]


def test_update_reports_reconnect_required_and_records_safe_checkpoint(tmp_path):
    seed(tmp_path, [item()])

    @contextmanager
    def expired():
        raise ConfigError("A fresh login is required but no email/password is configured.")
        yield  # pragma: no cover

    result = run_update(tmp_path, service_factory=expired, today=TODAY)
    assert result["status"] == "reconnect_required"
    assert "password" not in json.dumps(result).casefold()
    with sqlite3.connect(tmp_path / "nutrition.sqlite3") as connection:
        row = connection.execute(
            "SELECT stage,summary_json FROM update_events ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert row == ("reconnect_required", '{"status":"reconnect_required"}')


def test_resume_after_reconnect_is_idempotent(tmp_path):
    seed(tmp_path, [])
    first = run_update(tmp_path, service_factory=factory(), worker=Worker(), today=TODAY)
    second = run_update(tmp_path, service_factory=factory(), worker=Worker(), today=TODAY)
    assert first["status"].startswith("complete") and second["status"].startswith("complete")
    assert second["occurrences_added"] == second["weights_added"] == 0
    with sqlite3.connect(tmp_path / "nutrition.sqlite3") as connection:
        assert connection.execute("SELECT COUNT(*) FROM enrichment_versions").fetchone()[0] == 1


def branded_search(*foods):
    return {"foods": list(foods)}


def food(
    fdc_id=10,
    *,
    upc="1",
    description="Example",
    brand="Brand",
    published="2026-09-01",
    nutrients=None,
):
    return {
        "fdcId": fdc_id,
        "dataType": "Branded",
        "description": description,
        "brandOwner": brand,
        "gtinUpc": upc,
        "publicationDate": published,
        "foodNutrients": SEARCH_NUTRIENTS if nutrients is None else nutrients,
    }


DETAILS = {
    "foodNutrients": [
        {"nutrient": {"name": "Energy", "unitName": "kcal"}, "amount": 100},
        {"nutrient": {"name": "Protein", "unitName": "g"}, "amount": 5},
        {"nutrient": {"name": "Carbohydrate, by difference", "unitName": "g"}, "amount": 10},
        {"nutrient": {"name": "Total lipid (fat)", "unitName": "g"}, "amount": 4},
        {"nutrient": {"name": "Sodium, Na", "unitName": "mg"}, "amount": 50},
    ]
}
SEARCH_NUTRIENTS = [
    {
        "nutrientName": row["nutrient"]["name"],
        "unitName": row["nutrient"]["unitName"],
        "value": row["amount"],
    }
    for row in DETAILS["foodNutrients"]
]


class Response:
    def __init__(self, payload, status=200):
        self.payload, self.status_code = payload, status
        self.request = httpx.Request("GET", "https://api.nal.usda.gov/fdc/v1/test")

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("failed", request=self.request, response=self)

    def json(self):
        return self.payload


class Client:
    def __init__(self, search, details=DETAILS, *, error=None):
        self.search, self.details, self.error = search, details, error
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if self.error:
            raise self.error
        return Response(self.search if url.endswith("/foods/search") else self.details)


def candidate(**changes):
    base = {
        "source_food_id": "source-1",
        "food_name": "example",
        "brand": "brand",
        "portion_variants": [{"logged_portion": {"amount": 100, "unit": "g"}}],
        "missing_standard_nutrients": ["sodium_mg"],
    }
    return base | changes


def test_usda_exact_product_match_retains_provenance_and_reference():
    client = Client(branded_search(food()))
    evidence = UsdaFoodDataCentralWorker("private-key", client=client).research(candidate())
    assert evidence.identity_verified and evidence.portion_reconciled
    assert evidence.source_kind == "authoritative_database"
    assert evidence.nutrients["sodium_mg"] == 50
    assert evidence.url.endswith("/10/nutrients")
    assert date.fromisoformat(evidence.research_date) >= TODAY
    assert evidence.basis_amount == 100 and evidence.basis_unit == "g"


def test_usda_ambiguous_conflicting_products_fail_closed():
    worker = UsdaFoodDataCentralWorker(
        "key", client=Client(branded_search(food(10, upc="1"), food(11, upc="2")))
    )
    with pytest.raises(ResearchIssue) as caught:
        worker.research(candidate())
    assert caught.value.status == "ambiguous_match" and caught.value.needs_review


def test_usda_same_upc_uses_latest_record():
    client = Client(
        branded_search(
            food(10, upc="1", published="2025-01-01"),
            food(11, upc="1", published="2026-01-01"),
        )
    )
    evidence = UsdaFoodDataCentralWorker("key", client=client).research(candidate())
    assert "/11/" in evidence.url


def test_usda_blocks_branded_to_generic_and_close_name_substitutions():
    generic = food(description="Example", brand="", upc="") | {"dataType": "Foundation"}
    worker = UsdaFoodDataCentralWorker("key", client=Client(branded_search(generic)))
    with pytest.raises(ResearchIssue) as caught:
        worker.research(candidate())
    assert caught.value.status == "branded_generic_blocked"
    assert "generic" in caught.value.summary


def test_usda_conflicting_exact_records_require_review():
    conflicting = json.loads(json.dumps(SEARCH_NUTRIENTS))
    conflicting[0]["value"] = 200
    client = Client(
        branded_search(
            food(10, upc="1", published="2025-01-01"),
            food(11, upc="1", published="2026-01-01", nutrients=conflicting),
        )
    )

    with pytest.raises(ResearchIssue) as caught:
        UsdaFoodDataCentralWorker("key", client=client).research(candidate())
    assert caught.value.status == "conflicting_evidence" and caught.value.needs_review


def test_usda_serving_mismatch_is_explicit_not_guessed():
    worker = UsdaFoodDataCentralWorker("key", client=Client(branded_search(food())))
    evidence = worker.research(
        candidate(portion_variants=[{"logged_portion": {"amount": 1, "unit": "serving"}}])
    )
    assert not evidence.portion_reconciled
    assert "could not be reconciled" in evidence.concerns[0]


@pytest.mark.parametrize(
    "client,status",
    [
        (Client([], error=httpx.ReadTimeout("slow")), "timeout"),
        (Client([]), "malformed_response"),
    ],
)
def test_usda_timeout_and_malformed_response_are_sanitized(client, status):
    with pytest.raises(ResearchIssue) as caught:
        UsdaFoodDataCentralWorker("private-key", client=client).research(candidate())
    assert caught.value.status == status and caught.value.failure
    assert "private-key" not in str(caught.value)


def test_provider_missing_credentials_and_secret_storage(tmp_path):
    worker = ConfiguredResearchWorker(None)
    with pytest.raises(ResearchIssue, match="provider_not_configured"):
        worker.research(candidate())
    path = tmp_path / "usda"
    save_usda_api_key("secret-provider-key", path)
    assert load_usda_api_key(path) == "secret-provider-key"
    assert path.stat().st_mode & 0o777 == 0o600
    status = usda_configuration(path)
    assert status["configured"] and "secret-provider-key" not in json.dumps(status)


def test_provider_priority_keeps_direct_labels_and_prefers_usda_over_secondary():
    calls = []

    class Provider:
        def research(self, request):
            calls.append(request["source_food_id"])
            return replace(SEEDS["f0e5f283347d4cad3140e9b34bf8e2a8"], title="USDA fixture")

    worker = ConfiguredResearchWorker("key", provider=Provider())
    direct = {"source_food_id": "62b68f9ad47cee9da84c9074dd831d45"}
    assert worker.research(direct).source_kind == "retailer_label"
    secondary = {"source_food_id": "f0e5f283347d4cad3140e9b34bf8e2a8"}
    assert worker.research(secondary).title == "USDA fixture"
    assert calls == [secondary["source_food_id"]]


def test_research_issues_are_append_only_and_visible_in_review(tmp_path):
    seed(tmp_path, [item()])

    class Ambiguous:
        def research(self, request):
            raise ResearchIssue("ambiguous_match", "Fixture", "Two exact products.", True)

    result = process_research(tmp_path, worker=Ambiguous(), run_id="fixture")
    assert result["sent_to_review"] == result["unresolved"] == 1
    assert latest_research_issues(tmp_path)[0]["status"] == "ambiguous_match"
    with (
        sqlite3.connect(tmp_path / "nutrition.sqlite3") as connection,
        pytest.raises(sqlite3.IntegrityError),
    ):
        connection.execute("DELETE FROM research_attempts")


def test_missing_provider_does_not_break_successful_sync(tmp_path):
    seed(tmp_path, [item()])
    result = run_update(
        tmp_path,
        service_factory=factory(Remote()),
        worker=ConfiguredResearchWorker(None),
        today=TODAY,
    )
    assert result["status"] == "complete"
    assert result["unresolved"] == result["sent_to_review"] == 0
    assert result["resolved_by_representative_or_modeled_fallback"] == 1
    assert result["research_unavailable"] >= 1 and result["analytics_refreshed"]


def test_noninteractive_cli_reports_reconnect_without_prompt(tmp_path, monkeypatch, capsys):
    from loseit_mcp import update

    monkeypatch.setattr(
        update,
        "run_update",
        lambda data_dir: {"status": "reconnect_required", "through_date": None},
    )
    assert update.main(["--data-dir", str(tmp_path), "--json"]) == 3
    assert json.loads(capsys.readouterr().out)["status"] == "reconnect_required"


def test_dashboard_shows_connection_reconnect_and_provider_configuration(tmp_path, monkeypatch):
    seed(tmp_path, [item()])
    monkeypatch.setenv("LOSEIT_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(
        "loseit_mcp.reconnect.connection_status",
        lambda data_dir: {
            "status": "reconnect_required",
            "last_auth_check": None,
            "last_successful_update": None,
            "latest_diary_date": "2026-09-20",
        },
    )
    monkeypatch.setattr("loseit_mcp.reconnect.browser_profiles", lambda browser: [])
    monkeypatch.setattr(
        "loseit_mcp.update.run_update",
        lambda *a, **k: {
            "status": "reconnect_required",
            "error": "Lose It connection expired.",
            "through_date": None,
        },
    )
    at = AppTest.from_file(APP, default_timeout=30).run()
    assert any("Reconnect required" in row.value for row in at.markdown)
    next(button for button in at.button if button.label == "Update My Nutrition Data").click().run()
    assert any("connection expired" in row.value for row in at.warning)
    assert any(button.label == "Reconnect Lose It" for button in at.button)
    assert next(field for field in at.text_input if field.label == "liauth").proto.type == 1
    at.sidebar.radio[0].set_value("Settings & Targets").run()
    key = next(field for field in at.text_input if field.label == "FoodData Central API key")
    assert key.proto.type == 1


def test_current_schema_preserves_raw_immutability(tmp_path):
    seed(tmp_path, [item()])
    with NutritionRepository(tmp_path) as repo:
        assert repo.connection.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] == 8
        with pytest.raises(sqlite3.IntegrityError):
            repo.connection.execute("DELETE FROM raw_diary_snapshots")
