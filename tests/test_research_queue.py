"""Standard-gap worklists, safe source identity imports, and local-only execution."""

import json
import socket
import sqlite3

import pytest

from loseit_mcp.coverage import read_coverage
from loseit_mcp.repository import MIGRATIONS, STANDARD_NUTRIENTS, NutritionRepository
from loseit_mcp.research_queue import read_research_queue
from loseit_mcp.server import build_server
from loseit_mcp.sync_cli import main
from tests.test_readonly_repository import enrichment


def item(*, food_id="a", name="Example", brand="Brand", amount=1, nutrients=None, entry_id="e1"):
    return {
        "food_id": food_id,
        "food_name": name,
        "food_brand": brand,
        "entry_id": entry_id,
        "amount": amount,
        "unit": "serving",
        "servings": amount,
        "nutrients": nutrients if nutrients is not None else {"calories": 100 * amount},
        "serving_description": "Stored description",
    }


def seed(path, entries):
    with NutritionRepository(path) as repo:
        repo.ingest_day({"date": "2026-09-01", "entries": entries}, retrieved_at="2026-09-01")


def reviewed(record, nutrients=None):
    doc = enrichment(record["food_name"], record["brand"])
    doc["target"] = record["import_target"]
    doc["nutrition_basis"] = {
        "amount": 1,
        "unit": "serving",
        "description": "Reviewed fixture label",
    }
    doc["nutrients"] = [
        {"nutrient": n, "estimated_value": 0, "provenance": "researched_exact_product"}
        for n in (nutrients if nutrients is not None else record["missing_standard_nutrients"])
    ]
    return doc


def test_unknowns_ignored_and_standard_gaps_values_portions_preserved(tmp_path):
    full = dict.fromkeys(STANDARD_NUTRIENTS, 0)
    seed(
        tmp_path,
        [
            item(food_id="complete", nutrients=full),
            item(
                food_id="gap", entry_id="e2", nutrients={"calories": 0, "unknown_nutrient_99": 20}
            ),
        ],
    )
    report = read_research_queue(tmp_path)
    assert report["queue_count"] == 1
    row = report["foods"][0]
    assert row["source_food_id"] == "gap"
    assert row["source_nutrients"] == {"calories": 0}
    assert len(row["missing_standard_nutrients"]) == 8
    assert row["priority_score"] == 8
    assert row["source_serving"] is None
    assert row["portion_variants"][0]["logged_portion"] == {
        "amount": 1,
        "unit": "serving",
        "servings": 1,
        "description": "Stored description",
    }
    assert "unknown_nutrient_99" not in json.dumps(report)
    assert "unknown_nutrient_99" in read_coverage(tmp_path)["target_nutrients"]
    assert report["lifecycle_counts"]["complete"] == 1


def test_repeated_id_distinct_quantities_and_deterministic_output(tmp_path):
    seed(tmp_path, [item(amount=2), item(amount=4, entry_id="e2"), item(amount=4, entry_id="e3")])
    report = read_research_queue(tmp_path)
    row = report["foods"][0]
    assert report["queue_count"] == 1
    assert row["occurrence_count"] == 3
    assert row["priority_score"] == 24
    assert row["source_nutrients"] is None
    assert row["source_values_identical"] is False
    assert sorted(
        (v["logged_portion"]["amount"], v["source_nutrients"]["calories"], v["occurrence_count"])
        for v in row["portion_variants"]
    ) == [(2, 200, 1), (4, 400, 2)]
    assert row["first_seen"] == row["last_seen"] == "2026-09-01"
    assert read_research_queue(tmp_path) == report


def test_partial_then_complete_stable_id_import_roundtrip_and_future_sync(tmp_path):
    seed(tmp_path, [item(), item(entry_id="e2")])
    first = read_research_queue(tmp_path)["foods"][0]
    before = read_coverage(tmp_path)["nutrients"]["protein_g"]["source_reported"]
    with NutritionRepository(tmp_path) as repo:
        repo.import_enrichment(reviewed(first, ["protein_g"]))
    partial = read_research_queue(tmp_path)["foods"][0]
    assert partial["status"] == "partially_filled"
    assert "protein_g" not in partial["missing_standard_nutrients"]
    assert partial["source_nutrients"] == {"calories": 100}
    assert read_coverage(tmp_path)["nutrients"]["protein_g"]["source_reported"] == before
    # Each version is a complete replacement: include the prior reviewed value as well.
    with NutritionRepository(tmp_path) as repo:
        repo.import_enrichment(reviewed(partial, list(STANDARD_NUTRIENTS)[1:]))
        repo.ingest_day(
            {"date": "2026-09-02", "entries": [item(entry_id="e3", amount=3)]},
            retrieved_at="2026-09-02",
        )
        assert (
            repo.connection.execute("SELECT COUNT(*) FROM enrichment_versions").fetchone()[0] == 2
        )
        assert (
            repo.connection.execute("SELECT COUNT(*) FROM enrichment_review_contexts").fetchone()[0]
            == 2
        )
    assert read_research_queue(tmp_path)["queue_count"] == 0
    assert read_research_queue(tmp_path)["lifecycle_counts"] == {"complete": 1}


@pytest.mark.parametrize("field,value", [("confidence", "low"), ("manually_reviewed", False)])
def test_unreviewed_or_low_confidence_does_not_hide_gaps(tmp_path, field, value):
    seed(tmp_path, [item()])
    row = read_research_queue(tmp_path)["foods"][0]
    doc = reviewed(row)
    doc[field] = value
    with NutritionRepository(tmp_path) as repo:
        repo.import_enrichment(doc)
    result = read_research_queue(tmp_path)["foods"][0]
    assert result["missing_standard_nutrients"] == row["missing_standard_nutrients"]
    assert result["status"] == "needs_review"
    assert result["reference_status"][0]["eligible_for_reuse"] is False


def test_legacy_reviewed_exact_and_confirmed_alias_reused(tmp_path):
    seed(tmp_path, [item()])
    doc = enrichment("Example", "Brand")
    doc["nutrients"] = [
        {"nutrient": n, "estimated_value": 0, "provenance": "researched_exact_product"}
        for n in STANDARD_NUTRIENTS
    ]
    with NutritionRepository(tmp_path) as repo:
        repo.import_enrichment(doc)
    assert read_research_queue(tmp_path)["queue_count"] == 0
    seed(tmp_path, [item(food_id="b", name="Alias", entry_id="alias")])
    doc["aliases"] = [{"food_name": "Alias", "brand": "Brand", "manually_confirmed": True}]
    with NutritionRepository(tmp_path) as repo:
        repo.import_enrichment(doc)
    assert read_research_queue(tmp_path)["queue_count"] == 0


def test_target_does_not_leak_to_other_ids_same_name(tmp_path):
    seed(tmp_path, [item(), item(food_id="b", entry_id="e2")])
    row = next(r for r in read_research_queue(tmp_path)["foods"] if r["source_food_id"] == "a")
    with NutritionRepository(tmp_path) as repo:
        repo.import_enrichment(reviewed(row))
        repo.ingest_day(
            {"date": "2026-09-02", "entries": [item(food_id="c", entry_id="e3")]},
            retrieved_at="2026-09-02",
        )
    assert {r["source_food_id"] for r in read_research_queue(tmp_path)["foods"]} == {"b", "c"}
    other = next(r for r in read_research_queue(tmp_path)["foods"] if r["source_food_id"] == "b")
    with (
        NutritionRepository(tmp_path) as repo,
        pytest.raises(ValueError, match="different source food ID"),
    ):
        repo.import_enrichment(reviewed(other))


def test_wrong_id_stale_context_name_conflicts_and_atomic_rejection(tmp_path):
    seed(tmp_path, [item()])
    row = read_research_queue(tmp_path)["foods"][0]
    doc = reviewed(row)
    with NutritionRepository(tmp_path) as repo:
        doc["target"]["source_food_id"] = "wrong"
        with pytest.raises(ValueError, match="does not exist"):
            repo.import_enrichment(doc)
        doc["target"]["source_food_id"] = "a"
        doc["food_name"] = "Invented"
        with pytest.raises(ValueError, match="identity"):
            repo.import_enrichment(doc)
        doc["food_name"] = row["food_name"]
        repo.ingest_day(
            {"date": "2026-09-01", "entries": [item(amount=2)]}, retrieved_at="2026-09-02"
        )
        with pytest.raises(ValueError, match="context changed"):
            repo.import_enrichment(doc)
        assert (
            repo.connection.execute("SELECT COUNT(*) FROM enrichment_versions").fetchone()[0] == 0
        )


def test_conflicting_source_after_review_needs_review(tmp_path):
    seed(tmp_path, [item()])
    row = read_research_queue(tmp_path)["foods"][0]
    with NutritionRepository(tmp_path) as repo:
        repo.import_enrichment(reviewed(row))
        repo.ingest_day(
            {"date": "2026-09-01", "entries": [item(nutrients={"calories": 999})]},
            retrieved_at="2026-09-02",
        )
    result = read_research_queue(tmp_path)["foods"][0]
    assert result["status"] == "needs_review"
    assert result["reference_status"][0]["source_context_conflict"] is True


def test_id_change_clears_old_link(tmp_path):
    seed(tmp_path, [item()])
    doc = reviewed(read_research_queue(tmp_path)["foods"][0])
    with NutritionRepository(tmp_path) as repo:
        repo.import_enrichment(doc)
        repo.ingest_day(
            {"date": "2026-09-01", "entries": [item(food_id="different")]},
            retrieved_at="2026-09-02",
        )
    assert read_research_queue(tmp_path)["foods"][0]["source_food_id"] == "different"


def test_no_id_fallback_and_no_fabricated_basis(tmp_path):
    seed(tmp_path, [item(food_id=None), item(food_id=None, entry_id="e2", amount=2)])
    row = read_research_queue(tmp_path)["foods"][0]
    assert row["occurrence_count"] == 2
    assert row["import_target"] is None
    assert row["source_serving"] is None


def test_reviewed_id_with_changed_identity_requires_review(tmp_path):
    seed(tmp_path, [item()])
    doc = reviewed(read_research_queue(tmp_path)["foods"][0])
    with NutritionRepository(tmp_path) as repo:
        repo.import_enrichment(doc)
        repo.ingest_day(
            {"date": "2026-09-01", "entries": [item(name="Changed product")]},
            retrieved_at="2026-09-02",
        )
    row = read_research_queue(tmp_path)["foods"][0]
    assert row["status"] == "needs_review"
    assert row["matching_status"] == "ambiguous_source_identity_or_values"


def test_nonfinite_estimate_rejected_atomically(tmp_path):
    seed(tmp_path, [item()])
    doc = reviewed(read_research_queue(tmp_path)["foods"][0])
    doc["nutrients"][0]["estimated_value"] = float("inf")
    with NutritionRepository(tmp_path) as repo:
        with pytest.raises(ValueError, match="finite"):
            repo.import_enrichment(doc)
        assert (
            repo.connection.execute("SELECT COUNT(*) FROM enrichment_versions").fetchone()[0] == 0
        )


def test_local_json_no_network_auth_writes_and_readonly_schema1(tmp_path, monkeypatch, capsys):
    path = tmp_path / "nutrition.sqlite3"
    c = sqlite3.connect(path)
    c.executescript(MIGRATIONS[0])
    c.execute("INSERT INTO schema_version VALUES (1,'fixture')")
    c.commit()
    c.close()
    before = path.read_bytes()

    def forbidden(*args, **kwargs):
        raise AssertionError("Network, auth and writable repository forbidden")

    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr("loseit_mcp.sync_cli.load_settings", forbidden)
    monkeypatch.setattr("loseit_mcp.sync_cli.ReadOnlyLoseItService", forbidden)
    monkeypatch.setattr("loseit_mcp.sync_cli.NutritionRepository", forbidden)
    monkeypatch.setenv("LOSEIT_TOKEN", "private-test-credential")
    assert main(["--research-queue", "--json", "--data-dir", str(tmp_path)]) == 0
    output = capsys.readouterr().out
    assert json.loads(output)["queue_count"] == 0
    assert "private-test-credential" not in output
    assert path.read_bytes() == before
    assert read_coverage(tmp_path)["total_occurrences"] == 0
    assert main(["--research-queue", "--data-dir", str(tmp_path)]) == 0
    assert "local only" in capsys.readouterr().out
    assert path.read_bytes() == before


def test_conflicting_modes_never_sync(tmp_path):
    for flags in (
        ["--days", "7"],
        ["--coverage"],
        ["--show-queue"],
        ["--missing-only"],
        ["--import-enrichment", "file.json"],
    ):
        assert main(["--research-queue", "--data-dir", str(tmp_path), *flags]) == 2
    assert not (tmp_path / "nutrition.sqlite3").exists()


def test_cli_import_file_roundtrip(tmp_path, capsys):
    seed(tmp_path, [item()])
    record = read_research_queue(tmp_path)["foods"][0]
    path = tmp_path / "reviewed.json"
    path.write_text(json.dumps(reviewed(record)))
    assert main(["--import-enrichment", str(path), "--data-dir", str(tmp_path)]) == 0
    assert json.loads(capsys.readouterr().out)["version"] == 1
    assert read_research_queue(tmp_path)["queue_count"] == 0


def test_research_feature_does_not_expand_mcp_discovery(settings):
    assert {tool.name for tool in build_server(settings)._tool_manager.list_tools()} == {
        "describe_food",
        "get_diary",
        "get_diary_range",
        "get_weight_history",
        "search_food",
        "server_status",
        "whoami",
    }


def test_populated_json_omits_unrelated_snapshot_metadata(tmp_path, capsys, monkeypatch):
    with NutritionRepository(tmp_path) as repo:
        repo.ingest_day(
            {
                "date": "2026-09-01",
                "entries": [item()],
                "liauth": "fake-private-cookie",
                "account_email": "private@example.invalid",
            },
            retrieved_at="2026-09-01",
        )

    def forbidden(*args, **kwargs):
        raise AssertionError("No network or authentication allowed")

    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr("loseit_mcp.sync_cli.load_settings", forbidden)
    assert main(["--research-queue", "--json", "--data-dir", str(tmp_path)]) == 0
    output = capsys.readouterr().out
    assert json.loads(output)["queue_count"] == 1
    for private in ("liauth", "fake-private-cookie", "account_email", "private@example.invalid"):
        assert private not in output
