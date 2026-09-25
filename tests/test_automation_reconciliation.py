"""Daily automation, reconciliation, completion, inbox and static-export regressions."""

from __future__ import annotations

import json
import plistlib
import sqlite3
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from loseit_mcp.analytics import read_analytics
from loseit_mcp.publication import publish_snapshot, save_publication_settings
from loseit_mcp.repository import STANDARD_NUTRIENTS, NutritionRepository
from loseit_mcp.review_inbox import answer_question, needs_help, recent_answers
from loseit_mcp.scheduler import generate_configuration, run_scheduled_update
from loseit_mcp.theme import css, load_theme
from tests.test_research_queue import item, seed


def complete_item(**changes):
    nutrients = {name: 1.0 for name in STANDARD_NUTRIENTS}
    nutrients["calories"] = 100.0
    return item(nutrients=nutrients, **changes)


def test_launch_agent_configuration_is_native_local_and_secret_free(tmp_path):
    seed(tmp_path, [])
    configuration = generate_configuration(tmp_path, python="/safe/python")
    payload = plistlib.dumps(configuration).decode()
    assert configuration["StartCalendarInterval"] == {"Hour": 10, "Minute": 0}
    assert configuration["RunAtLoad"] is True
    assert configuration["Umask"] == 0o077
    assert configuration["ProgramArguments"][:4] == [
        "/safe/python",
        "-m",
        "loseit_mcp.scheduler",
        "run",
    ]
    assert "liauth" not in payload and "api_key" not in payload


def test_schema_six_upgrade_preserves_existing_occurrences(tmp_path):
    seed(tmp_path, [complete_item()])
    with sqlite3.connect(tmp_path / "nutrition.sqlite3") as connection:
        for table in (
            "source_nutrient_quality_findings",
            "nutrition_anomaly_findings",
            "nutrition_audit_runs",
            "occurrence_resolution_history",
            "formulation_nutrients",
            "formulation_servings",
            "food_formulations",
            "food_source_identities",
            "canonical_foods",
            "human_review_answers",
            "publication_events",
            "publication_settings_versions",
            "automation_events",
            "day_completion_observations",
        ):
            connection.execute(f"DROP TABLE {table}")
        connection.execute("DELETE FROM schema_version WHERE version>=7")
    with NutritionRepository(tmp_path) as repo:
        assert repo.connection.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] == 8
        assert repo.connection.execute("SELECT COUNT(*) FROM food_occurrences").fetchone()[0] == 1
        assert repo.connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name='automation_events'"
        ).fetchone()


def test_scheduled_catch_up_runs_once_and_records_success(tmp_path):
    seed(tmp_path, [])
    calls = []

    def runner(path, **kwargs):
        calls.append((path, kwargs))
        return {"status": "complete", "through_date": "2026-09-21"}

    before_ten = run_scheduled_update(
        tmp_path, now=datetime(2026, 9, 21, 9, tzinfo=UTC), runner=runner
    )
    assert before_ten["status"] == "not_due" and not calls
    catch_up = run_scheduled_update(
        tmp_path, now=datetime(2026, 9, 21, 11, tzinfo=UTC), runner=runner
    )
    assert catch_up["status"] == "complete"
    assert calls == [(tmp_path, {"trigger": "scheduled", "publish": True})]
    repeated = run_scheduled_update(
        tmp_path, now=datetime(2026, 9, 21, 15, tzinfo=UTC), runner=runner
    )
    assert repeated["status"] == "already_completed_today" and len(calls) == 1


def test_reconciliation_add_change_delete_and_idempotence(tmp_path):
    monday = "2026-09-14"
    breakfast = complete_item(entry_id="breakfast", name="Breakfast") | {"meal": "breakfast"}
    lunch = complete_item(entry_id="lunch", name="Lunch") | {"meal": "lunch"}
    dinner = complete_item(entry_id="dinner", name="Dinner") | {"meal": "dinner"}
    dessert = complete_item(entry_id="dessert", name="Dessert") | {"meal": "snacks"}
    with NutritionRepository(tmp_path) as repo:
        first = repo.ingest_day(
            {"date": monday, "entries": [breakfast, lunch]}, retrieved_at="2026-09-14T12:00:00Z"
        )
        revised = repo.ingest_day(
            {"date": monday, "entries": [breakfast, lunch, dinner, dessert]},
            retrieved_at="2026-09-16T12:00:00Z",
        )
        changed_dinner = dinner | {"amount": 2, "servings": 2, "meal": "snacks"}
        changed_dinner["nutrients"] = dinner["nutrients"] | {"calories": 200}
        changed = repo.ingest_day(
            {"date": monday, "entries": [breakfast, lunch, changed_dinner, dessert]},
            retrieved_at="2026-09-17T12:00:00Z",
        )
        deleted = repo.ingest_day(
            {"date": monday, "entries": [breakfast, lunch, changed_dinner]},
            retrieved_at="2026-09-18T12:00:00Z",
        )
        unchanged = repo.ingest_day(
            {"date": monday, "entries": [breakfast, lunch, changed_dinner]},
            retrieved_at="2026-09-19T12:00:00Z",
        )
        current = repo.connection.execute(
            "SELECT COUNT(*) FROM food_occurrences WHERE source_date=? AND is_current=1", (monday,)
        ).fetchone()[0]
        calories = repo.connection.execute(
            """SELECT SUM(n.value) FROM nutrient_observations n JOIN food_occurrences o
               ON o.id=n.occurrence_id WHERE o.source_date=? AND o.is_current=1
               AND n.nutrient='calories'""",
            (monday,),
        ).fetchone()[0]
        raw_count = repo.connection.execute(
            "SELECT COUNT(*) FROM raw_diary_snapshots WHERE source_date=?", (monday,)
        ).fetchone()[0]
    assert first["occurrences_added"] == 2
    assert revised["occurrences_added"] == 2 and revised["changed_days"] == 1
    assert changed["occurrences_changed"] == 1
    assert deleted["occurrences_removed"] == 1
    assert unchanged["unchanged_days"] == 1 and unchanged["raw_snapshots_added"] == 0
    assert current == 3 and calories == 400 and raw_count == 4


def test_explicit_completion_is_historical_and_insights_use_latest_complete(tmp_path):
    with NutritionRepository(tmp_path) as repo:
        repo.ingest_day(
            {"date": "2026-09-19", "entries": [complete_item()], "day_complete": True},
            retrieved_at="2026-09-19T12:00:00Z",
        )
        repo.ingest_day(
            {
                "date": "2026-09-20",
                "entries": [complete_item(entry_id="e2")],
                "day_complete": False,
            },
            retrieved_at="2026-09-20T12:00:00Z",
        )
    report = read_analytics(tmp_path, date(2026, 9, 19), date(2026, 9, 20), compare=False)
    assert report["completion"] == {
        "available": True,
        "latest_completed_date": "2026-09-19",
        "source_field": "$.day_complete",
        "meaning": "Explicit boolean returned by Lose It; no activity-based completion inference.",
    }
    assert report["insights_period"]["end"] == "2026-09-19"
    assert report["daily_metrics"][1]["source_day_status"] == "incomplete"
    with NutritionRepository(tmp_path) as repo:
        repo.ingest_day(
            {
                "date": "2026-09-20",
                "entries": [complete_item(entry_id="e2")],
                "day_complete": True,
            },
            retrieved_at="2026-09-21T12:00:00Z",
        )
        count = repo.connection.execute(
            "SELECT COUNT(*) FROM day_completion_observations WHERE source_date='2026-09-20'"
        ).fetchone()[0]
    assert count == 2


def test_missing_completion_field_remains_unknown_without_proxy(tmp_path):
    seed(tmp_path, [complete_item(name="Sugar Cookie")])
    report = read_analytics(tmp_path, date(2026, 9, 1), date(2026, 9, 1), compare=False)
    assert report["completion"]["available"] is False
    assert report["completion"]["latest_completed_date"] is None
    assert report["daily_metrics"][0]["source_day_status"] == "unknown"
    assert report["insights_period"]["basis"] == "coverage_aware_fallback"


def test_simple_human_answer_is_audited_and_suppresses_duplicate(tmp_path):
    seed(tmp_path, [item(name="Arnold Oatnut Bread", food_id="bread")])
    with NutritionRepository(tmp_path) as repo, repo.connection as c:
        c.execute(
            "INSERT INTO source_review_flags(source_food_id,status,note,created_at) VALUES ('bread','needs_review','Serving mismatch','2026-09-21')"
        )
    questions = needs_help(tmp_path)
    assert len(questions) == 1
    assert [choice["value"] for choice in questions[0]["choices"]] == [2, 4]
    answer_question(tmp_path, questions[0], 2)
    assert needs_help(tmp_path) == []
    assert recent_answers(tmp_path)[0]["status"] == "answered"
    with NutritionRepository(tmp_path) as repo:
        row = repo.connection.execute(
            "SELECT answer_json,disposition FROM human_review_answers"
        ).fetchone()
        assert json.loads(row[0]) == {"quantity": 2.0, "unit": "slices"}
        assert row[1] == "answered"


def test_static_export_is_sanitized_responsive_atomic_and_preserves_good_version(
    tmp_path, monkeypatch
):
    from loseit_mcp import publication

    seed(tmp_path, [complete_item(name="Sugar Cookie")])
    cloud = tmp_path / "CloudDocs"
    monkeypatch.setattr(publication, "ICLOUD_ROOT", cloud)
    destination = cloud / "Nutrition"
    save_publication_settings(tmp_path, destination, True)
    result = publish_snapshot(tmp_path, today=date(2026, 9, 1))
    assert result["status"] == "published"
    html_text = (destination / "nutrition_dashboard.html").read_text()
    json_text = (destination / "nutrition_snapshot.json").read_text()
    snapshot = json.loads(json_text)
    assert snapshot["schema_version"] == 1 and snapshot["read_only"] is True
    assert "@media(max-width:520px)" in html_text and "repeat(auto-fit,minmax" in html_text
    lowered = (html_text + json_text).lower()
    assert all(marker not in lowered for marker in ("liauth", "api_key", "raw_diary_snapshots"))
    assert "sugar cookie" in lowered  # Food names are data, not credential material.
    prior_html, prior_json = html_text, json_text
    scheduled_failure = run_scheduled_update(
        tmp_path,
        now=datetime(2026, 9, 2, 11, tzinfo=UTC),
        runner=lambda path, **kwargs: {"status": "failed"},
    )
    assert scheduled_failure["status"] == "failed"
    assert (destination / "nutrition_dashboard.html").read_text() == prior_html
    assert (destination / "nutrition_snapshot.json").read_text() == prior_json

    def fail(_snapshot):
        raise RuntimeError("fixture export failure")

    monkeypatch.setattr(publication, "_encoded", fail)
    failed = publish_snapshot(tmp_path, today=date(2026, 9, 1))
    assert failed["status"] == "failed"
    assert (destination / "nutrition_dashboard.html").read_text() == prior_html
    assert (destination / "nutrition_snapshot.json").read_text() == prior_json


def test_dashboard_css_has_fluid_multi_width_rules():
    stylesheet = css(load_theme())
    assert "clamp(" in stylesheet and "@media (max-width:900px)" in stylesheet
    assert "@media (max-width:560px)" in stylesheet and "flex-wrap:wrap" in stylesheet


def test_atomic_pair_rolls_back_if_second_replacement_fails(tmp_path, monkeypatch):
    from loseit_mcp import publication

    destination = tmp_path / "snapshot"
    destination.mkdir()
    html_path = destination / publication.HTML_NAME
    json_path = destination / publication.JSON_NAME
    html_path.write_bytes(b"old-html")
    json_path.write_bytes(b"old-json")
    original = publication.os.replace
    failed = False

    def flaky(source, target):
        nonlocal failed
        if Path(target) == json_path and not failed:
            failed = True
            raise OSError("fixture second replacement failure")
        return original(source, target)

    monkeypatch.setattr(publication.os, "replace", flaky)
    with pytest.raises(OSError, match="second replacement"):
        publication._atomic_pair(destination, b"new-html", b"new-json")
    assert html_path.read_bytes() == b"old-html"
    assert json_path.read_bytes() == b"old-json"
