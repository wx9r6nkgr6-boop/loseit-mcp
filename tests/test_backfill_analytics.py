"""No-network regressions for historical checkpoints and explicit analytic coverage."""

import json
import socket
import sqlite3
from datetime import date, timedelta

import pytest

from loseit_mcp.analytics import period_dates, read_analytics
from loseit_mcp.backfill import backfill, year_chunks
from loseit_mcp.coverage import read_coverage
from loseit_mcp.readonly_service import ReadOnlyLoseItService
from loseit_mcp.repository import STANDARD_NUTRIENTS, NutritionRepository
from loseit_mcp.research_queue import read_research_queue
from loseit_mcp.sync_cli import main
from tests.test_research_queue import item, reviewed


class Source:
    def __init__(self, fail=None, value=100):
        self.calls = []
        self.fail = fail
        self.value = value

    def get_diary_range(self, start, end, *, request_delay):
        self.calls.append((start, end, request_delay))
        if self.fail and len(self.calls) == 2:
            raise self.fail
        days = []
        day = date.fromisoformat(start)
        while day <= date.fromisoformat(end):
            days.append(
                {"date": day.isoformat(), "entries": [item(nutrients={"calories": self.value})]}
            )
            day += timedelta(days=1)
        return {"days": days}

    def get_weight_history(self, start, end):
        return {
            "start": start,
            "end": end,
            "entries": [{"date": start, "weight": self.value, "unit": "lb"}],
        }


def test_chunk_year_bounds_leap_and_future():
    ranges = year_chunks(2026, date(2026, 9, 8))
    assert ranges[0][0] == "2026-01-01"
    assert ranges[-1][1] == "2026-09-08"
    assert all(0 <= (date.fromisoformat(e) - date.fromisoformat(s)).days <= 30 for s, e in ranges)
    assert sum((date.fromisoformat(e) - date.fromisoformat(s)).days + 1 for s, e in ranges) == 251
    assert (
        sum(
            (date.fromisoformat(e) - date.fromisoformat(s)).days + 1
            for s, e in year_chunks(2024, date(2026, 1, 1))
        )
        == 366
    )
    with pytest.raises(ValueError):
        year_chunks(2027, date(2026, 9, 8))


@pytest.mark.parametrize(
    "failure",
    [
        OSError("offline"),
        RuntimeError("IncompatibleRemoteServiceException secret"),
        KeyboardInterrupt(),
    ],
)
def test_resume_checkpoint_and_refresh_changes(tmp_path, failure):
    sleeps = []
    with NutritionRepository(tmp_path) as repo:
        result = backfill(repo, Source(failure), 2026, today=date(2026, 2, 3), sleep=sleeps.append)
        assert result["successful_chunks"] == 1
        assert result["failed_chunks"] == 1
        assert "secret" not in json.dumps(result)
        if "Incompatible" in str(failure):
            assert "compatibility-check" in result["next_action"]
    with NutritionRepository(tmp_path) as repo:
        source = Source()
        resumed = backfill(repo, source, 2026, today=date(2026, 2, 3), sleep=sleeps.append)
        assert resumed["skipped_chunks"] == 1
        assert len(source.calls) == 1
        assert resumed["status"] == "complete"
        count = repo.connection.execute("SELECT COUNT(*) FROM food_occurrences").fetchone()[0]
        refresh = backfill(
            repo, Source(value=120), 2026, today=date(2026, 2, 3), sleep=sleeps.append
        )
        assert refresh["skipped_chunks"] == 0
        assert refresh["totals_completed_chunks_including_resumed"]["changed_raw_snapshots"] == 34
        assert (
            repo.connection.execute("SELECT COUNT(*) FROM food_occurrences").fetchone()[0]
            == count
            == 34
        )
        assert (
            repo.connection.execute("SELECT COUNT(*) FROM raw_diary_snapshots").fetchone()[0] == 68
        )
        assert (
            repo.connection.execute("SELECT MIN(weight) FROM weight_observations").fetchone()[0]
            == 120
        )
    assert sleeps and set(sleeps) == {1.0, 2.0}


def test_range_actual_request_pacing(settings, monkeypatch):
    waits = []
    monkeypatch.setattr("loseit_mcp.readonly_service.sleep", waits.append)

    class Delegate:
        def get_diary(self, day):
            return {"date": day.isoformat(), "entries": []}

    service = ReadOnlyLoseItService(settings, delegate=Delegate())
    assert len(service.get_diary_range("2026-01-01", "2026-01-03", request_delay=1.5)["days"]) == 3
    assert waits == [1.5, 1.5]


def test_backfill_pacing_validation_and_incomplete_response(tmp_path):
    with NutritionRepository(tmp_path) as repo:
        for delay in (0, float("nan"), float("inf")):
            with pytest.raises(ValueError):
                backfill(repo, Source(), 2026, request_delay=delay)

        class Incomplete(Source):
            def get_diary_range(self, *args, **kwargs):
                return {"days": []}

        assert (
            backfill(repo, Incomplete(), 2026, today=date(2026, 1, 1), sleep=lambda x: None)[
                "status"
            ]
            == "failed"
        )
        assert repo.connection.execute("SELECT COUNT(*) FROM food_occurrences").fetchone()[0] == 0


def put(repo, day, entries):
    repo.ingest_day({"date": day, "entries": entries}, retrieved_at="2026-09-08")


def full(calories=100, protein=10):
    return dict.fromkeys(STANDARD_NUTRIENTS, 0) | {"calories": calories, "protein_g": protein}


def test_daily_meal_food_period_averages_missing_and_weekends(tmp_path):
    with NutritionRepository(tmp_path) as repo:
        put(repo, "2026-09-04", [item(nutrients=full()) | {"meal": "breakfast"}])
        put(repo, "2026-09-05", [item(food_id="b", nutrients=full(200, 20)) | {"meal": "dinner"}])
        put(repo, "2026-09-06", [])
    report = read_analytics(
        tmp_path,
        date(2026, 9, 4),
        date(2026, 9, 6),
        protein_target=15,
        calorie_min=100,
        calorie_max=150,
    )
    assert report["logging_completeness"] == {
        "logged_days": 2,
        "calendar_days": 3,
        "missing_days": 1,
    }
    m = report["period_averages"]["calories"]
    assert m["average_per_logged_day"] == 150
    assert m["average_per_calendar_day"] is None
    assert m["recorded_contribution_per_calendar_day"] == 100
    assert report["daily_metrics"][-1]["nutrients"]["calories"][2] is None
    assert report["meal_summary"]["dinner"]["calories"]["period_share_pct"] == pytest.approx(66.667)
    foods = report["food_contributors"]
    assert foods["food_count"] == 2
    assert foods["catalog"][foods["rankings"]["calories"][0]]["source_food_id"] == "b"
    assert (
        report["weekday_weekend"]["weekend"]["nutrients"]["calories"]["average_per_logged_day"]
        == 200
    )
    assert report["consistency"]["protein_g"]["target_pct"] == 50
    assert report["consistency"]["longest_logged_streak"] == 2
    assert report["comparison"]["metrics"]["calories"]["percentage_change"] is None


def test_estimates_require_explicit_scaling_and_preserve_source(tmp_path):
    with NutritionRepository(tmp_path) as repo:
        put(repo, "2026-09-04", [item(amount=2)])
        record = read_research_queue(tmp_path)["foods"][0]
        doc = reviewed(record, ["protein_g"])
        doc["nutrients"][0]["estimated_value"] = 5
        repo.import_enrichment(doc)
    report = read_analytics(tmp_path, date(2026, 9, 4), date(2026, 9, 4))
    assert report["period_averages"]["protein_g"]["combined_usable"] is None
    with NutritionRepository(tmp_path) as repo:
        doc["nutrition_basis"]["occurrence_scaling"] = {
            "method": "logged_amount",
            "unit": "serving",
        }
        repo.import_enrichment(doc)
    report = read_analytics(tmp_path, date(2026, 9, 4), date(2026, 9, 4))
    protein = report["period_averages"]["protein_g"]
    assert protein["source_reported"] is None
    assert protein["estimated_or_enriched"] == protein["combined_usable"] == 10
    assert report["period_averages"]["calories"]["source_reported"] == 200
    assert report["data_quality"]["research_queue_count_global"] == 1
    assert report["data_quality"]["filled_by_scaled_enrichment_occurrences"] == 1


def test_weight_no_interpolation_mixed_units_and_rolling(tmp_path):
    with NutritionRepository(tmp_path) as repo:
        repo.ingest_weights(
            {
                "start": "2026-09-01",
                "end": "2026-09-07",
                "entries": [
                    {"date": f"2026-09-0{i}", "weight": 100 + i, "unit": "lb"} for i in (1, 3, 7)
                ],
            },
            retrieved_at="fixture",
        )
    w = read_analytics(tmp_path, date(2026, 9, 1), date(2026, 9, 7))["weight_summary"]
    assert w["observation_count"] == 3 and w["change"] == 6
    assert w["latest"]["rolling_7"] == pytest.approx(103.666667)
    assert w["first"]["rolling_7"] is None
    assert len(w["daily"]) == 3
    with NutritionRepository(tmp_path) as repo:
        repo.connection.execute(
            "UPDATE weight_observations SET unit='kg' WHERE source_date='2026-09-07'"
        )
        repo.connection.commit()
    assert (
        read_analytics(tmp_path, date(2026, 9, 1), date(2026, 9, 7))["weight_summary"]["change"]
        is None
    )


def test_history_retirement_and_unknowns_preserved(tmp_path):
    with NutritionRepository(tmp_path) as repo:
        put(
            repo,
            "2026-09-04",
            [item(nutrients=full() | {"unknown_nutrient_2": 77}), item(food_id="b", entry_id="e2")],
        )
        put(repo, "2026-09-04", [item(nutrients=full() | {"unknown_nutrient_2": 77})])
        assert (
            repo.connection.execute("SELECT COUNT(*) FROM raw_diary_snapshots").fetchone()[0] == 2
        )
    assert read_coverage(tmp_path)["total_occurrences"] == 1
    assert "unknown_nutrient_2" in read_coverage(tmp_path)["target_nutrients"]
    assert read_research_queue(tmp_path)["queue_count"] == 0
    report = read_analytics(tmp_path, date(2026, 9, 4), date(2026, 9, 4))
    assert report["period_averages"]["calories"]["combined_usable"] == 100


def test_comparison_complete_zero_denominator_and_quality(tmp_path):
    with NutritionRepository(tmp_path) as repo:
        put(repo, "2026-09-03", [item(nutrients=full(100, 0))])
        put(repo, "2026-09-04", [item(nutrients=full(200, 10) | {"sugar_g": 50})])
    report = read_analytics(tmp_path, date(2026, 9, 4), date(2026, 9, 4))
    assert report["comparison"]["metrics"]["calories"]["percentage_change"] == 100
    assert report["comparison"]["metrics"]["protein_g"]["percentage_change"] is None
    assert report["data_quality"]["flag_counts"]["sugar_g_exceeds_carb_g"] == 1


def test_analytics_local_json_no_credentials(tmp_path, monkeypatch, capsys):
    with NutritionRepository(tmp_path) as repo:
        put(repo, "2026-09-04", [item()])
    before = (tmp_path / "nutrition.sqlite3").read_bytes()

    def forbidden(*args, **kwargs):
        raise AssertionError("Network/auth/write forbidden")

    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr("loseit_mcp.sync_cli.load_settings", forbidden)
    monkeypatch.setattr("loseit_mcp.sync_cli.NutritionRepository", forbidden)
    monkeypatch.setenv("LOSEIT_TOKEN", "private-token")
    assert (
        main(
            [
                "--analytics",
                "--start",
                "2026-09-04",
                "--end",
                "2026-09-04",
                "--json",
                "--data-dir",
                str(tmp_path),
            ]
        )
        == 0
    )
    output = capsys.readouterr().out
    assert json.loads(output)["local_only"] is True
    assert "private-token" not in output
    assert before == (tmp_path / "nutrition.sqlite3").read_bytes()


@pytest.mark.parametrize(
    "period,expected",
    [
        ("last7", "2026-09-02"),
        ("last14", "2026-08-26"),
        ("last30", "2026-08-10"),
        ("week", "2026-09-07"),
        ("month", "2026-09-01"),
        ("ytd", "2026-01-01"),
    ],
)
def test_local_periods(period, expected):
    assert period_dates(period=period, today=date(2026, 9, 8)) == (
        date.fromisoformat(expected),
        date(2026, 9, 8),
    )


def test_migration_ddl_and_version_are_atomic(tmp_path, monkeypatch):
    with NutritionRepository(tmp_path):
        pass
    import loseit_mcp.repository as module

    monkeypatch.setattr(
        module, "MIGRATIONS", (*module.MIGRATIONS, "CREATE TABLE should_rollback(x); INVALID SQL;")
    )
    with pytest.raises(sqlite3.OperationalError):
        NutritionRepository(tmp_path)
    c = sqlite3.connect(tmp_path / "nutrition.sqlite3")
    assert (
        c.execute("SELECT name FROM sqlite_master WHERE name='should_rollback'").fetchone() is None
    )
    assert c.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] == 3
    c.close()


def test_partial_numeric_totals_and_calendar_views(tmp_path):
    with NutritionRepository(tmp_path) as repo:
        put(repo, "2026-09-04", [item(nutrients=full()), item(food_id="b", entry_id="e2")])
        assert (
            repo.connection.execute(
                "SELECT known_total FROM analytics_daily_source WHERE nutrient='calories'"
            ).fetchone()[0]
            == 200
        )
    report = read_analytics(tmp_path, date(2026, 9, 4), date(2026, 9, 4))
    m = report["period_averages"]["protein_g"]
    assert m["source_reported"] == 10
    assert m["missing_count"] == 1
    assert m["average_per_logged_day"] is None
    assert m["source_coverage_pct"] == 50
    assert report["calendar_summaries"]["month"][0]["nutrients"]["protein_g"]["missing_count"] == 1
    assert len(report["data_quality"]["foods"]) >= 1


def test_month_comparison_bounds_and_optional_name_grouping(tmp_path):
    with NutritionRepository(tmp_path) as repo:
        put(repo, "2026-03-01", [item(), item(food_id="b", entry_id="e2")])
    report = read_analytics(
        tmp_path, date(2026, 3, 1), date(2026, 3, 31), period="month", grouping="name"
    )
    assert report["comparison"]["prior_start"] == "2026-02-01"
    assert report["comparison"]["prior_end"] == "2026-02-28"
    assert report["food_contributors"]["food_count"] == 1


def test_targets_and_cli_conflicts_do_not_create_database(tmp_path):
    for options in (
        ["--analytics", "--backfill-year", "2026"],
        ["--analytics", "--days", "0"],
        ["--analytics", "--calorie-min", "2000"],
        ["--research-queue", "--analytics"],
    ):
        assert main(["--data-dir", str(tmp_path), *options]) == 2
    assert not (tmp_path / "nutrition.sqlite3").exists()


def test_partial_macro_contradiction_flag_without_changing_values(tmp_path):
    with NutritionRepository(tmp_path) as repo:
        put(repo, "2026-09-04", [item(nutrients={"calories": 2, "carb_g": 40})])
    report = read_analytics(tmp_path, date(2026, 9, 4), date(2026, 9, 4))
    assert (
        report["data_quality"]["flag_counts"]["known_macro_energy_exceeds_calories_review_only"]
        == 1
    )
    assert report["period_averages"]["calories"]["source_reported"] == 2
