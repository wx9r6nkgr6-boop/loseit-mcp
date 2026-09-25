"""Controlled orchestration/policy tests. All research and remote reads use fixtures."""

import copy
import json
import sqlite3
from contextlib import contextmanager
from dataclasses import replace
from datetime import date, timedelta

import pytest
from streamlit.testing.v1 import AppTest

from loseit_mcp.analytics import read_analytics
from loseit_mcp.enrichment_worker import SEEDS, Evidence, evaluate, make_proposal
from loseit_mcp.proposals import list_proposals, reader, review_proposal, source_context
from loseit_mcp.research_queue import read_research_queue
from loseit_mcp.theme import css, load_theme
from loseit_mcp.update import _apply_evidence, latest_update, run_update
from tests.test_dashboard_proposals import APP
from tests.test_research_queue import item, seed

TODAY = date(2026, 9, 20)
LABEL = {
    "calories": 100,
    "protein_g": 5,
    "carb_g": 10,
    "total_fat_g": 4,
    "saturated_fat_g": 1,
    "fiber_g": 1,
    "sugar_g": 2,
    "sodium_mg": 50,
    "cholesterol_mg": 0,
}


def evidence(**changes):
    return replace(
        Evidence(
            "a",
            "Example",
            "Brand",
            "https://label.example/product",
            "Fixture retailer label",
            "retailer_label",
            date.today().isoformat(),  # noqa: DTZ011 - local research date
            LABEL.copy(),
            1,
            "serving",
            "One fixture serving",
            True,
        ),
        **changes,
    )


def entry(food_id="a", name="Example", brand="Brand"):
    return item(
        food_id=food_id,
        name=name,
        brand=brand,
        nutrients={k: v for k, v in LABEL.items() if k != "sodium_mg"},
    )


class Worker:
    capability = "Fixture evidence"

    def research(self, candidate):
        assert set(candidate) == {"source_food_id", "food_name", "brand"}
        return evidence()


class Remote:
    def whoami(self):
        return {"user_id": "private-do-not-output"}

    def search_food(self, *args, **kwargs):
        return []

    def get_diary_range(self, start, end, **kwargs):
        day = date.fromisoformat(start)
        days = []
        while day <= date.fromisoformat(end):
            days.append({"date": day.isoformat(), "entries": [entry()]})
            day += timedelta(days=1)
        return {"days": days}

    def get_weight_history(self, start, end):
        return {"start": start, "end": end, "entries": [{"date": end, "weight": 100, "unit": None}]}


def factory(remote=None):
    @contextmanager
    def wrapped():
        yield remote or Remote()

    return wrapped


def test_update_happy_path_repeat_history_and_analytics(tmp_path):
    seed(tmp_path, [])
    stages = []
    first = run_update(
        tmp_path, service_factory=factory(), worker=Worker(), today=TODAY, progress=stages.append
    )
    assert first["status"] == "complete"
    assert first["occurrences_added"] == 8 and first["weights_added"] == 1
    assert first["automatically_enriched"] == first["nutrients_filled"] == 1
    assert first["analytics_refreshed"] and first["needs_review"] == 0
    assert first["through_date"] == TODAY.isoformat() and first["integrity"] == "ok"
    assert "private-do-not-output" not in json.dumps(first)
    assert any("Applying" in s for s in stages)
    proposal = list_proposals(tmp_path)[0]
    assert proposal["history"][-1]["actor"] == "policy"
    assert proposal["document"]["reference_url"] == evidence().url
    assert proposal["document"]["source_reference"] == evidence().title
    again = run_update(tmp_path, service_factory=factory(), worker=Worker(), today=TODAY)
    assert (
        again["occurrences_added"] == again["weights_added"] == again["automatically_enriched"] == 0
    )
    with reader(tmp_path) as c:
        assert c.execute("SELECT COUNT(*) FROM enrichment_versions").fetchone()[0] == 1
        actor = c.execute(
            "SELECT manually_reviewed,approval_actor FROM enrichment_versions"
        ).fetchone()
        assert tuple(actor) == (0, "policy")
        assert (
            c.execute(
                "SELECT COUNT(*) FROM nutrient_observations WHERE nutrient='sodium_mg'"
            ).fetchone()[0]
            == 0
        )
    with sqlite3.connect(tmp_path / "nutrition.sqlite3") as c:
        for table in ("enrichment_versions", "estimated_nutrients", "enrichment_review_contexts"):
            with pytest.raises(sqlite3.IntegrityError):
                c.execute(f"DELETE FROM {table}")
    assert latest_update(tmp_path)["analytics_refreshed"]
    assert read_analytics(tmp_path, TODAY, TODAY)["app_summary"]["insight_slots"]


@pytest.mark.parametrize(
    "stage", ["whoami", "search_food", "get_diary_range", "get_weight_history"]
)
def test_sync_failure_never_runs_worker_or_leaks_errors(tmp_path, stage):
    seed(tmp_path, [entry()])
    remote = Remote()

    def fail(*args, **kwargs):
        raise RuntimeError("secret-cookie-must-not-leak")

    setattr(remote, stage, fail)
    before = (tmp_path / "nutrition.sqlite3").read_bytes()
    result = run_update(tmp_path, service_factory=factory(remote), today=TODAY)
    assert result["status"] == "failed" and result["foods_checked"] == 0
    assert "secret-cookie" not in json.dumps(result)
    if stage in {"whoami", "search_food"}:
        assert before == (tmp_path / "nutrition.sqlite3").read_bytes()
    with reader(tmp_path) as c:
        assert c.execute("SELECT COUNT(*) FROM research_proposals").fetchone()[0] == 0


@pytest.mark.parametrize("response", [None, {}, "bad-json", float("nan")])
def test_missing_invalid_research_response(tmp_path, response):
    seed(tmp_path, [])

    class InvalidWorker:
        def research(self, candidate):
            return response

    result = run_update(tmp_path, service_factory=factory(), worker=InvalidWorker(), today=TODAY)
    assert result["automatically_enriched"] == 0
    assert result["unresolved"] == 0 and result["analytics_refreshed"]
    assert result["resolved_by_representative_or_modeled_fallback"] == 1
    assert result["sent_to_review"] == 0
    assert not list_proposals(tmp_path)


def test_worker_failure_isolated_and_recoverable(tmp_path):
    seed(tmp_path, [])

    class BrokenWorker:
        def research(self, candidate):
            raise RuntimeError("private external response")

    result = run_update(tmp_path, service_factory=factory(), worker=BrokenWorker(), today=TODAY)
    assert result["failures"] == 1 and result["analytics_refreshed"]
    assert "private external response" not in json.dumps(result)
    retry = run_update(tmp_path, service_factory=factory(), worker=Worker(), today=TODAY)
    assert retry["automatically_enriched"] == 0
    assert retry["remaining_research_queue"] == 0
    assert retry["library_status"]["source_identities"] == 1


def test_partial_worker_failure_does_not_block_other_food(tmp_path):
    other = entry(food_id="b", name="Other", brand="Brand")
    other["entry_id"] = "e2"
    seed(tmp_path, [entry(), other])

    class PartialWorker:
        capability = "Fixture mixed response"

        def research(self, candidate):
            if candidate["source_food_id"] == "a":
                raise RuntimeError("failed provider payload")
            return evidence(source_food_id="b", food_name="Other")

    result = run_update(tmp_path, service_factory=factory(), worker=PartialWorker(), today=TODAY)
    assert result["failures"] == 1
    assert result["automatically_enriched"] == result["nutrients_filled"] == 1
    assert result["analytics_refreshed"]


@pytest.mark.parametrize(
    "changes, phrase",
    [
        ({"confidence": "medium"}, "confidence"),
        ({"source_kind": "secondary_database"}, "Secondary"),
        ({"portion_reconciled": False}, "portion"),
        ({"basis_unit": "piece"}, "mismatch"),
        ({"identity_verified": False}, "identity"),
        ({"food_name": "Another product"}, "identity"),
        ({"nutrients": LABEL | {"calories": 200}}, "contradicts"),
        ({"nutrients": LABEL | {"saturated_fat_g": 40}}, "parent"),
        ({"nutrients": LABEL | {"sodium_mg": 20000}}, "bounds"),
        ({"research_date": "2020-01-01"}, "90 days"),
    ],
)
def test_policy_requires_review(tmp_path, changes, phrase):
    seed(tmp_path, [entry()])
    e = evidence(**changes)
    candidate = read_research_queue(tmp_path)["foods"][0]
    doc = make_proposal(candidate, e)
    with reader(tmp_path) as c:
        decision = evaluate(e, doc, source_context(c, "a"))
    assert not decision["auto_approve"]
    assert phrase in " ".join(decision["reasons"])
    assert _apply_evidence(tmp_path, candidate, e)[0] == "review"
    assert list_proposals(tmp_path)[0]["status"] == "needs_review"


@pytest.mark.parametrize("value", [-1, True, float("inf"), "5", None])
def test_invalid_numeric_evidence_rejected(tmp_path, value):
    seed(tmp_path, [entry()])
    with pytest.raises(ValueError):
        make_proposal(
            read_research_queue(tmp_path)["foods"][0],
            evidence(nutrients=LABEL | {"sodium_mg": value}),
        )


def test_auto_approval_atomic_rollback_and_pending_retry(tmp_path):
    seed(tmp_path, [entry()])
    candidate = read_research_queue(tmp_path)["foods"][0]
    with sqlite3.connect(tmp_path / "nutrition.sqlite3") as c:
        c.execute(
            "CREATE TRIGGER stop_review BEFORE INSERT ON proposal_reviews WHEN NEW.action='approved' BEGIN SELECT RAISE(ABORT,'fixture rollback'); END"
        )
    with pytest.raises(sqlite3.IntegrityError):
        _apply_evidence(tmp_path, candidate, evidence())
    with sqlite3.connect(tmp_path / "nutrition.sqlite3") as c:
        assert c.execute("SELECT COUNT(*) FROM enrichment_versions").fetchone()[0] == 0
        assert c.execute("SELECT COUNT(*) FROM research_decisions").fetchone()[0] == 0
        c.execute("DROP TRIGGER stop_review")
    assert _apply_evidence(tmp_path, candidate, evidence()) == ("automatic", 1)


def test_rejected_deferred_and_append_only(tmp_path):
    seed(tmp_path, [entry()])
    candidate = read_research_queue(tmp_path)["foods"][0]
    e = evidence(confidence="medium")
    _apply_evidence(tmp_path, candidate, e)
    pid = list_proposals(tmp_path)[0]["id"]
    review_proposal(tmp_path, pid, "deferred", note="Need actual label")
    assert _apply_evidence(tmp_path, candidate, e)[0] == "existing"
    review_proposal(tmp_path, pid, "rejected", note="Wrong formulation")
    assert _apply_evidence(tmp_path, candidate, evidence())[0] == "review"
    with sqlite3.connect(tmp_path / "nutrition.sqlite3") as c:
        for table in ("research_decisions", "proposal_reviews", "update_events"):
            with pytest.raises(sqlite3.IntegrityError):
                # update_events needs at least one row for a trigger to execute.
                if table == "update_events":
                    c.execute(
                        "INSERT INTO update_events VALUES (1,'fixture','started','{}','fixture')"
                    )
                c.execute(f"DELETE FROM {table}")


@pytest.mark.parametrize("food_id, automatic", [(key, key.startswith("62b68")) for key in SEEDS])
def test_four_seed_policy(tmp_path, food_id, automatic):
    e = SEEDS[food_id]
    source = e.nutrients.copy()
    missing = (
        "sodium_mg"
        if food_id.startswith("4f53")
        else "cholesterol_mg"
        if food_id.startswith("49c")
        else "fiber_g"
        if food_id.startswith("f0e")
        else "carb_g"
    )
    source.pop(missing)
    if food_id.startswith("4f53"):
        source = {"calories": 160}
    row = item(food_id=food_id, name=e.food_name, brand=e.brand, nutrients=source)
    row["unit"] = e.basis_unit
    seed(tmp_path, [row])
    candidate = read_research_queue(tmp_path)["foods"][0]
    doc = make_proposal(candidate, e)
    with reader(tmp_path) as c:
        result = evaluate(e, doc, source_context(c, food_id), today=TODAY)
    assert result["auto_approve"] is automatic


def test_arnold_contradiction_never_auto_approved(tmp_path):
    source = {
        "calories": 480,
        "carb_g": 84,
        "total_fat_g": 10,
        "fiber_g": 8,
        "sugar_g": 12,
        "sodium_mg": 300,
    }
    row = item(name="Bread Oatnut", brand="Arnold", amount=2, nutrients=source)
    row["unit"] = "slice"
    seed(tmp_path, [row])
    e = evidence(
        food_name="Bread Oatnut",
        brand="Arnold",
        basis_unit="slice",
        nutrients={
            "calories": 120,
            "carb_g": 21,
            "total_fat_g": 2.5,
            "fiber_g": 2,
            "sugar_g": 3,
            "protein_g": 4,
            "sodium_mg": 150,
        },
    )
    assert _apply_evidence(tmp_path, read_research_queue(tmp_path)["foods"][0], e)[0] == "review"


def test_theme_tokens_and_safe_fallback(tmp_path):
    theme = load_theme()
    assert not theme["fallback"] and theme["colors"]["accentPrimary"] == "#FF3BBF"
    assert theme["radius"] == 8 and "NeonCardModifier" in theme["mapping"]["radius"]
    assert "<style>" in css(theme)
    assert load_theme(tmp_path / "missing.json")["fallback"]
    malformed = tmp_path / "invalid.json"
    malformed.write_text(json.dumps({"colors": {"accentPrimary": "</style>"}}))
    assert load_theme(malformed)["fallback"]


def test_button_uses_shared_operation_no_json_transfer(tmp_path, monkeypatch):
    seed(tmp_path, [entry()])
    monkeypatch.setenv("LOSEIT_DATA_DIR", str(tmp_path))
    calls = []

    def update(path, progress):
        calls.append(path)
        return run_update(
            path, service_factory=factory(), worker=Worker(), today=TODAY, progress=progress
        )

    monkeypatch.setattr("loseit_mcp.update.run_update", update)
    at = AppTest.from_file(APP, default_timeout=30).run()
    next(b for b in at.button if b.label == "Update My Nutrition Data").click().run()
    assert calls and not at.error and not at.exception
    assert any("Data updated through" in s.value for s in at.success)
    at.sidebar.radio[0].set_value("Review & Enrichment").run()
    assert next(s for s in at.selectbox if s.label == "Review view").value == "Needs Review"


def test_document_units_and_provenance_rechecked(tmp_path):
    seed(tmp_path, [entry()])
    candidate = read_research_queue(tmp_path)["foods"][0]
    doc = make_proposal(candidate, evidence())
    wrong = copy.deepcopy(doc)
    wrong["nutrients"][0]["unit"] = "g"
    with pytest.raises(ValueError), reader(tmp_path) as c:
        evaluate(evidence(), wrong, source_context(c, "a"))
    wrong = doc | {"reference_url": "https://other.example"}
    with reader(tmp_path) as c:
        assert not evaluate(evidence(), wrong, source_context(c, "a"))["auto_approve"]
