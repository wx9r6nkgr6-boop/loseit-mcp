"""A deliberately narrow, read-only boundary around the upstream service.

The wrapped :class:`LoseItService` retains mutation code for ease of rebasing on
upstream.  Neither MCP registration nor the user-facing CLI ever receives that
object; they receive this facade, whose public API contains reads only.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Self

from .config import Settings
from .service import LoseItService, _parse_date

MAX_DIARY_RANGE_DAYS = 31

STANDARD_NUTRIENTS = (
    "calories",
    "protein_g",
    "carb_g",
    "total_fat_g",
    "saturated_fat_g",
    "fiber_g",
    "sugar_g",
    "sodium_mg",
    "cholesterol_mg",
)


class ReadOnlyLoseItService:
    """Expose only operations proven to use Lose It! read paths."""

    __slots__ = ("__delegate",)

    def __init__(self, settings: Settings, *, delegate: LoseItService | None = None):
        self.__delegate = delegate or LoseItService(settings)

    def close(self) -> None:
        self.__delegate.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def whoami(self) -> dict[str, Any]:
        return self.__delegate.whoami()

    def search_food(
        self, query: str, limit: int = 15, *, detail: bool = True
    ) -> list[dict[str, Any]]:
        return self.__delegate.search_food(query, limit=limit, detail=detail)

    def describe_food(self, food_id: str) -> dict[str, Any]:
        return self.__delegate.describe_food(food_id)

    def get_diary(self, when: str | date | None = None) -> dict[str, Any]:
        return self.__delegate.get_diary(when)

    def get_diary_range(self, start_date: str, end_date: str) -> dict[str, Any]:
        """Read each diary day in an inclusive, bounded calendar range."""
        start = _required_iso_date(start_date, "start_date")
        end = _required_iso_date(end_date, "end_date")
        if end < start:
            raise ValueError(
                f"start_date {start.isoformat()} is after end_date {end.isoformat()}."
            )
        day_count = (end - start).days + 1
        if day_count > MAX_DIARY_RANGE_DAYS:
            raise ValueError(
                f"Requested {day_count} calendar days; get_diary_range allows at most "
                f"{MAX_DIARY_RANGE_DAYS} inclusive days per call."
            )

        days: list[dict[str, Any]] = []
        current = start
        while current <= end:
            day = self.get_diary(current)
            totals, coverage = _daily_totals(day.get("entries") or [])
            day["daily_totals"] = totals
            day["nutrient_coverage"] = coverage
            day["entry_ids_are_informational_only"] = True
            days.append(day)
            current += timedelta(days=1)

        return {
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "inclusive": True,
            "day_count": day_count,
            "days": days,
        }

    def get_weight_history(
        self,
        start: str | date | None = None,
        end: str | date | None = None,
        days: int = 30,
    ) -> dict[str, Any]:
        return self.__delegate.get_weight_history(start=start, end=end, days=days)


def _required_iso_date(value: str, field: str) -> date:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} is required and must use YYYY-MM-DD.")
    parsed = _parse_date(value)
    if parsed is None or value.strip().lower() in {"today", "yesterday"}:
        raise ValueError(f"{field} must be an explicit YYYY-MM-DD date.")
    return parsed


def _daily_totals(
    entries: list[dict[str, Any]],
) -> tuple[dict[str, float | None], dict[str, dict[str, int | bool]]]:
    """Sum reported values while making partial coverage impossible to miss."""
    observed: dict[str, list[float]] = {key: [] for key in STANDARD_NUTRIENTS}
    for entry in entries:
        nutrients = entry.get("nutrients") or {}
        if entry.get("calories") is not None and "calories" not in nutrients:
            nutrients = {**nutrients, "calories": entry["calories"]}
        for key, value in nutrients.items():
            if isinstance(value, int | float):
                observed.setdefault(key, []).append(float(value))

    totals: dict[str, float | None] = {}
    coverage: dict[str, dict[str, int | bool]] = {}
    for key, values in observed.items():
        totals[key] = round(sum(values), 2) if values else None
        coverage[key] = {
            "reported_entries": len(values),
            "total_entries": len(entries),
            "complete": len(values) == len(entries),
        }
    return totals, coverage

