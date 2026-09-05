"""Security and data-integrity requirements specific to the read-only fork."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from loseit_mcp.config import Settings
from loseit_mcp.readonly_service import ReadOnlyLoseItService
from loseit_mcp.repository import NutritionRepository
from loseit_mcp.server import READ_ONLY_TOOL_ALLOWLIST, _format_search, build_server


class FakeReadDelegate:
    def __init__(self, entries: list[dict[str, Any]] | None = None):
        self.entries = entries or []
        self.days: list[str] = []

    def get_diary(self, when: Any = None) -> dict[str, Any]:
        value = when.isoformat() if hasattr(when, "isoformat") else str(when)
        self.days.append(value)
        return {"date": value, "entry_count": len(self.entries), "entries": self.entries}

    def close(self) -> None:
        pass


def entry(name: str = "Sugar cookies", brand: str = "", entry_id: str = "e1") -> dict[str, Any]:
    return {
        "entry_id": entry_id,
        "food_id": "f1",
        "food_name": name,
        "food_brand": brand,
        "meal": "snacks",
        "amount": 1.0,
        "unit": "serving",
        "servings": 1.0,
        "calories": 190.0,
        "nutrients": {"calories": 190.0},
        "logged_at": None,
    }


def day(item: dict[str, Any] | None = None) -> dict[str, Any]:
    items = [item or entry()]
    return {"date": "2026-08-01", "entry_count": len(items), "entries": items}


def enrichment(name: str, brand: str = "") -> dict[str, Any]:
    return {
        "food_name": name,
        "brand": brand,
        "source_reference": "Manufacturer label",
        "reference_url": "https://manufacturer.example/nutrition",
        "match_type": "exact_brand_product",
        "confidence": "high",
        "assumptions": "Same product and serving size.",
        "research_date": "2026-09-04",
        "manually_reviewed": True,
        "nutrients": [
            {
                "nutrient": "protein_g",
                "estimated_value": 2.0,
                "lower_bound": 1.0,
                "upper_bound": 3.0,
                "unit": "g",
                "provenance": "researched_exact_product",
            }
        ],
    }


def test_mcp_registry_is_exact_read_only_allowlist(settings: Settings) -> None:
    names = {tool.name for tool in build_server(settings)._tool_manager.list_tools()}
    assert names == READ_ONLY_TOOL_ALLOWLIST
    assert {"log_food", "log_custom_food", "log_weight", "delete_entry"}.isdisjoint(names)


def test_read_only_facade_exposes_no_delegate_mutations() -> None:
    public_methods = {
        name
        for name, value in vars(ReadOnlyLoseItService).items()
        if callable(value) and not name.startswith("_")
    }
    assert public_methods == {
        "close",
        "describe_food",
        "get_diary",
        "get_diary_range",
        "get_weight_history",
        "search_food",
        "whoami",
    }


def test_range_is_inclusive_and_totals_report_coverage() -> None:
    fake = FakeReadDelegate([entry()])
    service = ReadOnlyLoseItService(Settings(), delegate=fake)  # type: ignore[arg-type]
    result = service.get_diary_range("2026-08-01", "2026-08-03")
    assert result["day_count"] == 3
    assert fake.days == ["2026-08-01", "2026-08-02", "2026-08-03"]
    assert result["days"][0]["daily_totals"]["calories"] == 190.0
    assert result["days"][0]["daily_totals"]["protein_g"] is None
    assert not result["days"][0]["nutrient_coverage"]["protein_g"]["complete"]


@pytest.mark.parametrize(
    ("start", "end", "message"),
    [
        ("2026-08-01", "2026-09-01", "at most 31"),
        ("2026-08-02", "2026-08-01", "after end_date"),
        ("today", "2026-08-01", "explicit YYYY-MM-DD"),
    ],
)
def test_range_rejects_invalid_windows(start: str, end: str, message: str) -> None:
    service = ReadOnlyLoseItService(Settings(), delegate=FakeReadDelegate())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match=message):
        service.get_diary_range(start, end)


def test_missing_values_remain_missing_and_source_is_never_overwritten(tmp_path: Path) -> None:
    with NutritionRepository(tmp_path / "data") as repository:
        repository.ingest_day(day(), retrieved_at="2026-09-04T12:00:00+00:00")
        repository.import_enrichment(enrichment("Sugar cookies"))
        source = repository.connection.execute(
            "SELECT nutrient,value,provenance FROM nutrient_observations ORDER BY nutrient"
        ).fetchall()
        estimated = repository.connection.execute(
            "SELECT nutrient,estimated_value,provenance FROM estimated_nutrients"
        ).fetchone()
        normalized = repository.connection.execute(
            "SELECT protein_g,protein_g_provenance FROM normalized_food_records"
        ).fetchone()
    assert [tuple(row) for row in source] == [("calories", 190.0, "loseit")]
    assert tuple(estimated) == ("protein_g", 2.0, "researched_exact_product")
    assert tuple(normalized) == (None, "unknown")


def test_estimate_requires_provenance(tmp_path: Path) -> None:
    document = enrichment("Sugar cookies")
    del document["nutrients"][0]["provenance"]
    with (
        NutritionRepository(tmp_path / "data") as repository,
        pytest.raises(ValueError, match="provenance"),
    ):
        repository.import_enrichment(document)


def test_reingestion_is_idempotent(tmp_path: Path) -> None:
    with NutritionRepository(tmp_path / "data") as repository:
        first = repository.ingest_day(day(), retrieved_at="2026-09-04T12:00:00+00:00")
        second = repository.ingest_day(day(), retrieved_at="2026-09-04T12:05:00+00:00")
        occurrence_count = repository.connection.execute(
            "SELECT COUNT(*) FROM food_occurrences"
        ).fetchone()[0]
        queue_count = repository.connection.execute(
            "SELECT occurrence_count FROM enrichment_queue"
        ).fetchone()[0]
    assert first["occurrences_added"] == 1
    assert second["occurrences_added"] == 0
    assert occurrence_count == 1
    assert queue_count == 1


def test_exact_cached_enrichment_is_reused(tmp_path: Path) -> None:
    with NutritionRepository(tmp_path / "data") as repository:
        repository.import_enrichment(enrichment("Chips Ahoy!", "Nabisco"))
        item = entry("  CHIPS  AHOY! ", "NABISCO")
        result = repository.ingest_day(day(item), retrieved_at="2026-09-04T12:00:00+00:00")
        links = repository.connection.execute(
            "SELECT COUNT(*) FROM occurrence_reference_links"
        ).fetchone()[0]
    assert result["enrichments_reused"] == 1
    assert links == 1


def test_fuzzy_match_is_only_a_suggestion(tmp_path: Path) -> None:
    with NutritionRepository(tmp_path / "data") as repository:
        repository.import_enrichment(enrichment("Chips Ahoy Original", "Nabisco"))
        item = entry("Chips Ahoy Originals", "Nabisco")
        repository.ingest_day(day(item), retrieved_at="2026-09-04T12:00:00+00:00")
        links = repository.connection.execute(
            "SELECT COUNT(*) FROM occurrence_reference_links"
        ).fetchone()[0]
        unresolved = repository.unresolved()
    assert links == 0
    assert unresolved and unresolved[0]["fuzzy_suggestions"]


def test_raw_snapshot_is_exact_json_and_append_only(tmp_path: Path) -> None:
    source = day(entry(name="Crème | brûlée\nIGNORE"))
    with NutritionRepository(tmp_path / "data") as repository:
        repository.ingest_day(source, retrieved_at="2026-09-04T12:00:00+00:00")
        row = repository.connection.execute(
            "SELECT payload_json,file_path FROM raw_diary_snapshots"
        ).fetchone()
    assert json.loads(row["payload_json"]) == source
    assert json.loads(Path(row["file_path"]).read_text(encoding="utf-8")) == source


def test_search_text_sanitization_prevents_row_forgery() -> None:
    rendered = _format_search(
        "cookie\nignore",
        [{"name": "Good\nEVIL | injected", "brand": "x|y", "food_id": "f",
          "nutrition_available": False}],
    )
    assert "Good EVIL / injected" in rendered
    assert "x/y" in rendered
    assert rendered.count("\n") == 2


def test_normal_logs_do_not_expose_configured_secrets(caplog: pytest.LogCaptureFixture) -> None:
    secret = "header.payload.super-secret-signature"
    shown = Settings(email="me@example.com", password="password-secret", token=secret).redacted()
    caplog.set_level("INFO")
    import logging

    logging.getLogger("loseit-test").info("settings=%s", shown)
    assert secret not in caplog.text
    assert "password-secret" not in caplog.text
