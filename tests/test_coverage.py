"""Coverage arithmetic, local-only execution and source/reference separation."""

import json
import socket
import sqlite3
from pathlib import Path

import pytest

from loseit_mcp.coverage import format_coverage, read_coverage
from loseit_mcp.repository import STANDARD_NUTRIENTS, NutritionRepository
from loseit_mcp.sync_cli import main
from tests.test_readonly_repository import enrichment


def seed(path: Path, items: list[dict]) -> None:
    with NutritionRepository(path) as repo:
        repo.ingest_day(
            {
                "date": "2026-09-01",
                "entries": [
                    {
                        "entry_id": str(i),
                        "food_name": item.get("name", "Sample"),
                        "food_brand": item.get("brand", ""),
                        "food_id": item.get("id"),
                        "nutrients": item["nutrients"],
                    }
                    for i, item in enumerate(items)
                ],
            },
            retrieved_at="2026-09-01T12:00:00Z",
        )


def test_source_missing_zero_dynamic_and_weighting(tmp_path: Path) -> None:
    seed(
        tmp_path,
        [
            {"id": "a", "nutrients": {"calories": 100, "protein_g": 0, "potassium_mg": 10}},
            {"id": "b", "nutrients": {"calories": 300}},
        ],
    )
    report = read_coverage(tmp_path)
    protein = report["nutrients"]["protein_g"]
    assert protein["source_reported"] == {
        "occurrences_with_value": 1,
        "total_occurrences": 2,
        "percentage": 50,
        "missing_occurrences": 1,
        "calorie_weighted_percentage": 25,
    }
    assert protein["combined_usable"]["missing_occurrences"] == 1
    assert report["nutrients"]["fiber_g"]["source_reported"]["occurrences_with_value"] == 0
    assert "potassium_mg" in report["target_nutrients"]
    assert report["foods"][0]["source_food_id"] == "b"
    assert "potassium_mg" in report["foods"][0]["nutrients"]


@pytest.mark.parametrize("values", [[100, None], [0, 0], [100, -1]])
def test_no_manufactured_calorie_weighting(tmp_path: Path, values: list) -> None:
    seed(tmp_path, [{"nutrients": {"calories": value, "protein_g": 1}} for value in values])
    result = read_coverage(tmp_path)["nutrients"]["protein_g"]
    assert result["combined_usable"]["calorie_weighted_percentage"] is None


def test_linked_latest_estimates_union_bounds_and_unlinked(tmp_path: Path) -> None:
    seed(
        tmp_path,
        [
            {"id": "a", "name": "Sample", "nutrients": {"calories": 100, "protein_g": 5}},
            {"id": "a", "name": "Sample", "nutrients": {"calories": 100}},
            {"id": "b", "name": "Other", "nutrients": {"calories": 100}},
        ],
    )
    with NutritionRepository(tmp_path) as repo:
        doc = enrichment("Sample")
        repo.import_enrichment(doc)
        doc["nutrients"] = [
            {
                "nutrient": "protein_g",
                "estimated_value": 3,
                "provenance": "researched_generic_food",
            },
            {
                "nutrient": "fiber_g",
                "lower_bound": 1,
                "upper_bound": 2,
                "provenance": "inferred_from_calories",
            },
        ]
        repo.import_enrichment(doc)
        repo.import_enrichment(enrichment("Unlinked food"))
    report = read_coverage(tmp_path)
    protein = report["nutrients"]["protein_g"]
    assert protein["source_reported"]["occurrences_with_value"] == 1
    assert protein["estimated_or_enriched"]["occurrences_with_value"] == 2
    assert protein["estimated_or_enriched"]["fills_source_missing_occurrences"] == 1
    assert protein["combined_usable"]["occurrences_with_value"] == 2
    assert protein["combined_usable"]["missing_occurrences"] == 1
    assert report["nutrients"]["fiber_g"]["estimated_or_enriched"]["bounds_only_occurrences"] == 2
    assert (
        next(f for f in report["foods"] if f["source_food_id"] == "a")["reference_status"][
            "linked_occurrences"
        ]
        == 2
    )
    # Replacing a version removes the old nutrient availability; versions are not unioned.
    with NutritionRepository(tmp_path) as repo:
        doc["nutrients"] = [doc["nutrients"][1]]
        repo.import_enrichment(doc)
    assert (
        read_coverage(tmp_path)["nutrients"]["protein_g"]["combined_usable"][
            "occurrences_with_value"
        ]
        == 1
    )


def test_grouping_id_repeats_fallback_and_priority(tmp_path: Path) -> None:
    seed(
        tmp_path,
        [
            {"id": "a", "name": "Old name", "nutrients": {}},
            {"id": "a", "name": "New name", "nutrients": {"calories": 10}},
            {"id": "b", "name": "New name", "nutrients": {}},
            {"name": "No id", "nutrients": {}},
            {"name": "NO ID", "nutrients": {}},
            {"name": "No id", "brand": "Distinct", "nutrients": {}},
        ],
    )
    report = read_coverage(tmp_path)
    assert report["total_foods"] == 4
    food = next(f for f in report["foods"] if f["source_food_id"] == "a")
    assert food["occurrence_count"] == 2
    assert food["priority_score"] == 17
    assert food["nutrients"]["calories"]["source_reported"]["missing_occurrences"] == 1
    assert [f["priority_score"] for f in report["foods"]] == [18, 17, 9, 9]


def test_complete_source_unresolved_and_missing_filter(tmp_path: Path) -> None:
    seed(
        tmp_path,
        [
            {"id": "complete", "nutrients": dict.fromkeys(STANDARD_NUTRIENTS, 0)},
            {"id": "missing", "nutrients": {}},
        ],
    )
    report = read_coverage(tmp_path)
    complete = next(f for f in report["foods"] if f["source_food_id"] == "complete")
    assert complete["reference_status"]["queue_statuses"] == ["unresolved"]
    assert complete["missing_nutrient_count"] == 0
    assert complete["priority_score"] == 0
    assert report["unresolved_foods_with_complete_source"] == 1
    assert report["foods_with_complete_standard_source"] == 1
    filtered = read_coverage(tmp_path, missing_only=True)
    assert len(filtered["foods"]) == 1
    assert filtered["nutrients"] == report["nutrients"]


def test_cli_local_only_json_no_credentials_or_mutations(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    seed(tmp_path, [{"id": "a", "nutrients": {"calories": 100}}])
    path = tmp_path / "nutrition.sqlite3"
    before = path.read_bytes()

    def forbidden(*args, **kwargs):
        raise AssertionError("Coverage must not use network, auth, or writable repository")

    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr("loseit_mcp.sync_cli.load_settings", forbidden)
    monkeypatch.setattr("loseit_mcp.sync_cli.ReadOnlyLoseItService", forbidden)
    monkeypatch.setattr("loseit_mcp.sync_cli.NutritionRepository", forbidden)
    monkeypatch.setenv("LOSEIT_TOKEN", "secret-token-must-not-appear")
    assert main(["--coverage", "--json", "--data-dir", str(tmp_path)]) == 0
    output = capsys.readouterr().out
    result = json.loads(output)
    assert result["local_only"] is True
    assert "secret-token" not in output
    assert all(word not in output for word in ("cookie", "source_entry_id", "logged_at", "token"))
    assert path.read_bytes() == before
    assert main(["--coverage", "--data-dir", str(tmp_path)]) == 0
    assert "Unresolved reference status does not mean missing nutrition" in capsys.readouterr().out
    assert path.read_bytes() == before


def test_missing_database_not_created_and_conflicting_flags(tmp_path: Path, capsys) -> None:
    assert main(["--coverage", "--data-dir", str(tmp_path / "absent")]) == 2
    assert not (tmp_path / "absent").exists()
    assert main(["--coverage", "--days", "7", "--data-dir", str(tmp_path)]) == 2
    assert main(["--missing-only", "--data-dir", str(tmp_path)]) == 2
    assert not (tmp_path / "nutrition.sqlite3").exists()


def test_empty_database_and_sqlite_readonly(tmp_path: Path, monkeypatch) -> None:
    with NutritionRepository(tmp_path):
        pass
    import loseit_mcp.coverage as module

    original = module._report

    def check(connection, **kwargs):
        with pytest.raises(sqlite3.OperationalError):
            connection.execute("DELETE FROM food_occurrences")
        return original(connection, **kwargs)

    monkeypatch.setattr(module, "_report", check)
    report = read_coverage(tmp_path)
    assert report["total_occurrences"] == 0
    assert report["nutrients"]["calories"]["source_reported"]["percentage"] is None
    assert "n/a" in format_coverage(report)
