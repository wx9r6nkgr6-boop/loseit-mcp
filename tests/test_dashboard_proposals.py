"""Local proposal lifecycle, strict validation, UI smoke and no-auth regression tests."""

import copy
import json
import socket
import sqlite3
from datetime import date, timedelta
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from loseit_mcp.analytics import read_analytics
from loseit_mcp.dashboard_cli import command
from loseit_mcp.insights import generate_insights, trend_series
from loseit_mcp.proposals import (
    ProposalError,
    flag_source,
    import_proposal,
    list_proposals,
    parse_proposal,
    read_settings,
    review_context,
    review_proposal,
    save_settings,
)
from loseit_mcp.repository import NutritionRepository
from loseit_mcp.research_queue import read_research_queue
from tests.test_research_queue import item, reviewed, seed

APP = str(Path(__file__).parents[1] / "src/loseit_mcp/dashboard.py")


def proposal(path):
    record = read_research_queue(path)["foods"][0]
    doc = reviewed(record)
    doc.pop("manually_reviewed")
    doc["schema_version"] = 1
    doc["nutrition_basis"]["occurrence_scaling"] = {"method": "logged_amount", "unit": "serving"}
    for n in doc["nutrients"]:
        n["unit"] = (
            "mg"
            if n["nutrient"].endswith("_mg")
            else "kcal"
            if n["nutrient"] == "calories"
            else "g"
        )
    return doc


def imported(path, doc=None):
    return import_proposal(path, json.dumps(doc or proposal(path)).encode())["id"]


def report(path):
    return read_analytics(path, date(2026, 9, 1), date(2026, 9, 1))


def test_pending_approval_atomic_history_and_source_preservation(tmp_path):
    seed(tmp_path, [item()])
    before = report(tmp_path)
    with sqlite3.connect(tmp_path / "nutrition.sqlite3") as c:
        source = c.execute("SELECT * FROM nutrient_observations").fetchall()
        raw = c.execute("SELECT payload_json FROM raw_diary_snapshots").fetchall()
    pid = imported(tmp_path)
    assert report(tmp_path) == before
    assert list_proposals(tmp_path)[0]["status"] == "ready"
    names = [n["nutrient"] for n in proposal(tmp_path)["nutrients"]]
    review_proposal(tmp_path, pid, "approve", selected=names)
    assert read_research_queue(tmp_path)["queue_count"] == 0
    assert report(tmp_path)["period_averages"]["protein_g"]["estimated_or_enriched"] == 0
    assert list_proposals(tmp_path)[0]["status"] == "approved"
    with sqlite3.connect(tmp_path / "nutrition.sqlite3") as c:
        assert c.execute("SELECT * FROM nutrient_observations").fetchall() == source
        assert c.execute("SELECT payload_json FROM raw_diary_snapshots").fetchall() == raw
        assert c.execute("SELECT manually_reviewed FROM enrichment_versions").fetchone()[0] == 1
        with pytest.raises(sqlite3.IntegrityError):
            c.execute("DELETE FROM proposal_reviews")
    with pytest.raises(ProposalError):
        review_proposal(tmp_path, pid, "approve", selected=names)


def test_partial_approval_retains_previously_reviewed_values(tmp_path):
    seed(tmp_path, [item()])
    doc = proposal(tmp_path)
    pid = imported(tmp_path, doc)
    review_proposal(tmp_path, pid, "approve", selected=["protein_g"])
    assert list_proposals(tmp_path)[0]["status"] == "partially_approved"
    assert (
        "protein_g" not in read_research_queue(tmp_path)["foods"][0]["missing_standard_nutrients"]
    )
    remaining = [n["nutrient"] for n in doc["nutrients"] if n["nutrient"] != "protein_g"]
    review_proposal(tmp_path, pid, "approve", selected=remaining)
    versions = review_context(tmp_path, "a")["enrichment_versions"]
    assert len(versions) == 2
    assert len(versions[0]["nutrients"]) == 1
    assert len(versions[1]["nutrients"]) == 8
    assert read_research_queue(tmp_path)["queue_count"] == 0


@pytest.mark.parametrize("action", ["rejected", "deferred"])
def test_reject_defer_do_not_change_nutrition(tmp_path, action):
    seed(tmp_path, [item()])
    before = report(tmp_path)
    pid = imported(tmp_path)
    review_proposal(tmp_path, pid, action, note="Fixture review")
    p = list_proposals(tmp_path)[0]
    assert p["status"] == action
    assert p["history"][-1]["note"] == "Fixture review"
    assert report(tmp_path) == before


def test_edit_then_approve_records_user_edit_and_keeps_original(tmp_path):
    seed(tmp_path, [item()])
    original = proposal(tmp_path)
    pid = imported(tmp_path, original)
    edited = copy.deepcopy(original)
    edited["nutrients"][0]["estimated_value"] = 7
    review_proposal(
        tmp_path,
        pid,
        "approve",
        selected=["protein_g"],
        edited_document=edited,
        note="Corrected fixture transcription",
    )
    p = list_proposals(tmp_path)[0]
    assert p["history"][-1]["user_edited"] == 1
    assert p["document"]["nutrients"][0]["estimated_value"] == 7
    with sqlite3.connect(tmp_path / "nutrition.sqlite3") as c:
        saved = json.loads(c.execute("SELECT document_json FROM research_proposals").fetchone()[0])
        assert saved == original


def test_stale_context_and_source_mismatch_block_approval(tmp_path):
    seed(tmp_path, [item()])
    doc = proposal(tmp_path)
    pid = imported(tmp_path, doc)
    seed(tmp_path, [item(amount=2)])
    assert list_proposals(tmp_path)[0]["status"] == "needs_review"
    with pytest.raises(ProposalError, match="context"):
        review_proposal(tmp_path, pid, "approve", selected=["protein_g"])
    doc["target"]["source_food_id"] = "missing"
    with pytest.raises(ProposalError, match="ID"):
        imported(tmp_path, doc)


def test_duplicate_and_conflicting_proposals(tmp_path):
    seed(tmp_path, [item()])
    doc = proposal(tmp_path)
    pid = imported(tmp_path, doc)
    assert import_proposal(tmp_path, json.dumps(doc).encode()) == {"id": pid, "duplicate": True}
    changed = copy.deepcopy(doc)
    changed["nutrients"][0]["estimated_value"] = 9
    other = imported(tmp_path, changed)
    assert all(p["status"] == "needs_review" for p in list_proposals(tmp_path))
    with pytest.raises(ProposalError, match="conflict"):
        review_proposal(tmp_path, pid, "approve", selected=["protein_g"])
    review_proposal(tmp_path, other, "rejected")
    review_proposal(tmp_path, pid, "approve", selected=["protein_g"])


@pytest.mark.parametrize(
    "kind",
    [
        "nan",
        "negative",
        "bool",
        "unit",
        "nutrient",
        "provenance",
        "duplicate",
        "bounds",
        "extra",
        "url",
        "hash",
        "size",
        "key",
    ],
)
def test_strict_validation(tmp_path, kind):
    seed(tmp_path, [item()])
    doc = proposal(tmp_path)
    n = doc["nutrients"][0]
    if kind == "nan":
        n["estimated_value"] = float("nan")
    if kind == "negative":
        n["estimated_value"] = -1
    if kind == "bool":
        n["estimated_value"] = True
    if kind == "unit":
        n["unit"] = "mg"
    if kind == "nutrient":
        n["nutrient"] = "unknown_nutrient_7"
    if kind == "provenance":
        n.pop("provenance")
    if kind == "duplicate":
        doc["nutrients"][1] = n.copy()
    if kind == "bounds":
        n.update(lower_bound=8, upper_bound=1)
    if kind == "extra":
        doc["liauth"] = "fake-private"
    if kind == "url":
        doc["reference_url"] = "javascript:alert(1)"
    if kind == "hash":
        doc["target"]["source_context_sha256"] = "wrong"
    data = json.dumps(doc).encode()
    if kind == "size":
        data = b" " * 1000001
    if kind == "key":
        data = b'{"schema_version":1,"schema_version":1}'
    with pytest.raises(ProposalError):
        parse_proposal(data)


def test_review_transaction_rolls_back_enrichment_if_history_insert_fails(tmp_path):
    seed(tmp_path, [item()])
    pid = imported(tmp_path)
    with sqlite3.connect(tmp_path / "nutrition.sqlite3") as c:
        c.execute(
            "CREATE TRIGGER fail_review BEFORE INSERT ON proposal_reviews BEGIN SELECT RAISE(ABORT,'fixture failure'); END"
        )
    with pytest.raises(sqlite3.IntegrityError):
        review_proposal(tmp_path, pid, "approve", selected=["protein_g"])
    with sqlite3.connect(tmp_path / "nutrition.sqlite3") as c:
        assert c.execute("SELECT COUNT(*) FROM enrichment_versions").fetchone()[0] == 0
        assert c.execute("SELECT COUNT(*) FROM source_reference_targets").fetchone()[0] == 0


def test_source_unreliable_flag_is_annotation_and_settings_persist(tmp_path):
    seed(tmp_path, [item()])
    original = report(tmp_path)["period_averages"]
    flag_source(tmp_path, "a", "unreliable", "Portion ambiguity")
    assert report(tmp_path)["data_quality"]["flag_counts"]["manual_source_unreliable"] == 1
    assert report(tmp_path)["period_averages"] == original
    flag_source(tmp_path, "a", "cleared", "Reviewed context")
    assert "manual_source_unreliable" not in report(tmp_path)["data_quality"]["flag_counts"]
    settings = read_settings(tmp_path) | {
        "protein_target": 80,
        "fiber_target": 25,
        "sodium_limit": 2000,
    }
    save_settings(tmp_path, settings)
    assert read_settings(tmp_path) == settings
    assert len(review_context(tmp_path, "a")["manual_flags"]) == 2


def test_rolling_series_does_not_connect_incomplete_days(tmp_path):
    with NutritionRepository(tmp_path) as repo:
        for i in range(9):
            d = date(2026, 8, 24) + timedelta(days=i)
            nutrients = {"calories": 100} if i != 7 else {"protein_g": 3}
            repo.ingest_day(
                {"date": d.isoformat(), "entries": [item(nutrients=nutrients)]},
                retrieved_at="fixture",
            )
    data = read_analytics(tmp_path, date(2026, 8, 24), date(2026, 9, 1))
    series = trend_series(data, "calories")
    assert series[6]["rolling_7"] == 100
    assert series[7]["complete_total"] is None
    assert series[8]["rolling_7"] is None
    assert series[6]["complete_segment"] != series[8]["complete_segment"]
    assert generate_insights(data) == generate_insights(data)
    assert not any("increased" in s or "decreased" in s for s in data["insights"])
    assert data["app_summary"]["insight_slots"] == data["insights"]


def test_dashboard_local_pages_reuse_analytics_and_no_auth(tmp_path, monkeypatch):
    seed(tmp_path, [item()])
    monkeypatch.setenv("LOSEIT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("LOSEIT_TOKEN", "fake-private-credential")
    from loseit_mcp import analytics

    actual = analytics.read_analytics
    calls = []

    def reused(*args, **kwargs):
        calls.append((args, kwargs))
        return actual(*args, **kwargs)

    def forbidden(*args, **kwargs):
        raise AssertionError("Network or auth forbidden")

    monkeypatch.setattr(analytics, "read_analytics", reused)
    monkeypatch.setattr("loseit_mcp.config.load_settings", forbidden)
    # Warm plotting dependencies before guarding network primitives.
    import altair  # noqa: F401

    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    before = (tmp_path / "nutrition.sqlite3").read_bytes()
    at = AppTest.from_file(APP, default_timeout=30).run()
    assert not at.exception and not at.error
    at.sidebar.selectbox[0].set_value("YTD").run()
    for section in (
        "Trends",
        "Meals",
        "Foods",
        "Weight",
        "Coverage & Data Quality",
        "Review & Enrichment",
        "Settings & Targets",
    ):
        at.sidebar.radio[0].set_value(section).run()
        assert not at.exception and not at.error, section
    assert calls
    assert before == (tmp_path / "nutrition.sqlite3").read_bytes()
    assert "fake-private-credential" not in str(at)


def test_dashboard_upload_and_approve_fixture(tmp_path, monkeypatch):
    seed(tmp_path, [item()])
    monkeypatch.setenv("LOSEIT_DATA_DIR", str(tmp_path))
    at = AppTest.from_file(APP, default_timeout=30).run()
    at.sidebar.radio[0].set_value("Review & Enrichment").run()
    at.file_uploader[0].set_value(
        ("fixture.json", json.dumps(proposal(tmp_path)).encode(), "application/json")
    ).run()
    next(b for b in at.button if b.label == "Import as pending proposal").click().run()
    assert list_proposals(tmp_path)[0]["status"] == "ready"
    next(s for s in at.selectbox if s.label == "Review view").set_value("Ready to Approve").run()
    next(c for c in at.checkbox if c.label.startswith("I verified")).check().run()
    next(b for b in at.button if b.label == "Approve selected nutrients").click().run()
    assert not at.exception and not at.error
    assert read_research_queue(tmp_path)["queue_count"] == 0


def test_launcher_enforces_local_privacy():
    args = command(8501, Path("/private/tmp/fixture"))
    assert "--server.address=127.0.0.1" in args
    assert "--browser.gatherUsageStats=false" in args
    assert "--server.enableXsrfProtection=true" in args
    assert "--server.enableCORS=true" in args
    assert "--server.fileWatcherType=none" in args


def test_low_confidence_approved_version_stays_distinguishable(tmp_path):
    seed(tmp_path, [item()])
    doc = proposal(tmp_path)
    doc["confidence"] = "low"
    pid = imported(tmp_path, doc)
    review_proposal(tmp_path, pid, "approve", selected=[n["nutrient"] for n in doc["nutrients"]])
    assert read_research_queue(tmp_path)["foods"][0]["status"] == "needs_review"
    assert report(tmp_path)["period_averages"]["protein_g"]["estimated_or_enriched"] is None


def test_retained_provenance_cannot_be_silently_relabelled(tmp_path):
    seed(tmp_path, [item()])
    doc = proposal(tmp_path)
    pid = imported(tmp_path, doc)
    review_proposal(tmp_path, pid, "approve", selected=["protein_g"])
    edited = copy.deepcopy(doc)
    edited["reference_url"] = "https://different.example/label"
    with pytest.raises(ProposalError, match="evidence"):
        review_proposal(tmp_path, pid, "approve", selected=["sodium_mg"], edited_document=edited)
    assert len(review_context(tmp_path, "a")["enrichment_versions"]) == 1


def test_dashboard_settings_save_and_date_presets(tmp_path, monkeypatch):
    seed(tmp_path, [item()])
    monkeypatch.setenv("LOSEIT_DATA_DIR", str(tmp_path))
    at = AppTest.from_file(APP, default_timeout=30).run()
    for preset in ("Last 7 days", "Last 14 days", "Last 30 days", "Current month", "YTD"):
        at.sidebar.selectbox[0].set_value(preset).run()
        assert not at.error and not at.exception
    at.sidebar.radio[0].set_value("Settings & Targets").run()
    next(n for n in at.number_input if n.label == "Protein target (g)").set_value(80)
    next(b for b in at.button if b.label == "Save local targets").click().run()
    assert read_settings(tmp_path)["protein_target"] == 80
    assert not at.error and not at.exception
