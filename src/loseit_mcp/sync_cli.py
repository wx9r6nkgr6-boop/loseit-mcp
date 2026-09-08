"""Manual, idempotent date-range sync into the local repository."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from .config import load_settings
from .readonly_service import MAX_DIARY_RANGE_DAYS, ReadOnlyLoseItService
from .repository import NutritionRepository


def default_data_dir() -> Path:
    configured = os.environ.get("LOSEIT_DATA_DIR")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".local" / "share" / "loseit-readonly"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="loseit-sync",
        description="Read Lose It! data and update the local nutrition repository.",
    )
    window = parser.add_mutually_exclusive_group()
    window.add_argument("--start", help="Inclusive first day, YYYY-MM-DD.")
    window.add_argument(
        "--days", type=int, default=None, help="Number of days ending today (maximum 31)."
    )
    parser.add_argument("--end", help="Inclusive last day, YYYY-MM-DD; requires --start.")
    parser.add_argument("--data-dir", type=Path, default=default_data_dir())
    parser.add_argument("--no-weights", action="store_true", help="Skip weight-history retrieval.")
    parser.add_argument(
        "--show-queue", action="store_true", help="Print unresolved enrichment items and exit."
    )
    parser.add_argument(
        "--import-enrichment", type=Path, help="Import one reviewed enrichment JSON document."
    )
    parser.add_argument("--coverage", action="store_true", help="Report local nutrition coverage only.")
    parser.add_argument("--research-queue", action="store_true", help="Report standard nutrient research gaps locally.")
    parser.add_argument("--json", action="store_true", help="Emit coverage or research queue as JSON.")
    parser.add_argument("--missing-only", action="store_true", help="Only show foods with coverage gaps.")
    parser.add_argument("--backfill-year", type=int, help="Resume or refresh one calendar year, capped at local today.")
    parser.add_argument("--request-delay", type=float, default=1.0, help="Backfill inter-day delay, minimum 0.25 seconds.")
    parser.add_argument("--chunk-delay", type=float, default=2.0, help="Backfill inter-chunk delay, minimum 0.25 seconds.")
    parser.add_argument("--analytics", action="store_true", help="Local-only analytics; no authentication or network.")
    parser.add_argument("--period", choices=["last7", "last14", "last30", "week", "month", "ytd"])
    parser.add_argument("--group-foods", choices=["id", "name"], default="id")
    parser.add_argument("--protein-target", type=float)
    parser.add_argument("--calorie-min", type=float)
    parser.add_argument("--calorie-max", type=float)
    return parser


def _range(args: argparse.Namespace) -> tuple[date, date]:
    if args.start:
        if not args.end:
            raise ValueError("--start requires --end")
        try:
            start = date.fromisoformat(args.start)
            end = date.fromisoformat(args.end)
        except ValueError as exc:
            raise ValueError("--start and --end must use YYYY-MM-DD") from exc
    elif args.end:
        raise ValueError("--end requires --start")
    else:
        days = 7 if args.days is None else args.days
        if days < 1 or days > MAX_DIARY_RANGE_DAYS:
            raise ValueError(f"--days must be between 1 and {MAX_DIARY_RANGE_DAYS}")
        end = date.today()  # noqa: DTZ011 - source calendar uses local dates
        start = end - timedelta(days=days - 1)
    if end < start:
        raise ValueError(f"start {start} is after end {end}")
    if (end - start).days + 1 > MAX_DIARY_RANGE_DAYS:
        raise ValueError(f"a sync may cover at most {MAX_DIARY_RANGE_DAYS} inclusive days")
    return start, end


def _load_document(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError("enrichment document must be a JSON object")
    return value


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        modes = [args.coverage, args.research_queue, args.analytics, args.backfill_year is not None,
                 args.show_queue, args.import_enrichment is not None]
        if sum(bool(m) for m in modes) > 1:
            raise ValueError("Choose exactly one reporting, import, or backfill mode")
        if not args.analytics and (args.period or args.protein_target is not None or args.calorie_min is not None or args.calorie_max is not None or args.group_foods != "id"):
            raise ValueError("Analytics options require --analytics")
        if args.json and not (args.coverage or args.research_queue or args.analytics or args.backfill_year is not None):
            raise ValueError("--json requires a report or backfill mode")
        if args.analytics:
            if args.no_weights or args.missing_only or (args.period and (args.days is not None or args.start or args.end)):
                raise ValueError("Conflicting analytics options")
            from .analytics import format_analytics, period_dates, read_analytics

            start, end = period_dates(period=args.period or f"last{args.days if args.days is not None else 7}",
                                      start=date.fromisoformat(args.start) if args.start else None,
                                      end=date.fromisoformat(args.end) if args.end else None)
            report = read_analytics(args.data_dir, start, end, period=args.period or "custom",
                                    grouping=args.group_foods, protein_target=args.protein_target,
                                    calorie_min=args.calorie_min, calorie_max=args.calorie_max)
            print(json.dumps(report, ensure_ascii=False, allow_nan=False, separators=(",", ":")) if args.json else format_analytics(report))
            return 0
        if args.backfill_year is not None:
            if args.start or args.end or args.days is not None or args.no_weights or args.missing_only:
                raise ValueError("Backfill always includes matching weights and its own year date range")
            from .backfill import backfill, year_chunks

            year_chunks(args.backfill_year)  # Validate before opening writable storage/auth.
            with NutritionRepository(args.data_dir) as repository, ReadOnlyLoseItService(load_settings()) as service:
                report = backfill(repository, service, args.backfill_year,
                                  request_delay=args.request_delay, chunk_delay=args.chunk_delay,
                                  progress=lambda message: print(message, file=sys.stderr, flush=True))
            print(json.dumps(report, ensure_ascii=False, allow_nan=False, indent=None if args.json else 2))
            return 0 if report["status"] == "complete" else 2
        if args.missing_only and not args.coverage:
            raise ValueError("--missing-only requires --coverage")
        if args.research_queue:
            if (args.coverage or args.show_queue or args.import_enrichment or args.start or args.end
                    or args.days is not None or args.no_weights or args.missing_only):
                raise ValueError("--research-queue cannot be combined with other modes or sync options")
            from .research_queue import format_research_queue, read_research_queue

            report = read_research_queue(args.data_dir)
            print(json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False)
                  if args.json else format_research_queue(report))
            return 0
        if args.coverage:
            if (args.show_queue or args.import_enrichment or args.start or args.end
                    or args.days is not None or args.no_weights):
                raise ValueError("--coverage cannot be combined with sync, queue, or enrichment options")
            from .coverage import format_coverage, read_coverage

            report = read_coverage(args.data_dir, missing_only=args.missing_only)
            print(
                json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False)
                if args.json else format_coverage(report)
            )
            return 0
        with NutritionRepository(args.data_dir) as repository:
            if args.show_queue:
                print(json.dumps(repository.unresolved(), indent=2, ensure_ascii=False))
                return 0
            if args.import_enrichment:
                print(json.dumps(repository.import_enrichment(_load_document(args.import_enrichment)), indent=2))
                return 0

            start, end = _range(args)
            run_id = repository.begin_sync(start.isoformat(), end.isoformat())
            retrieved_at = datetime.now(UTC).isoformat(timespec="seconds")
            try:
                settings = load_settings()
                with ReadOnlyLoseItService(settings) as service:
                    payload = service.get_diary_range(start.isoformat(), end.isoformat())
                    summary: dict[str, Any] = repository.ingest_range(
                        payload, retrieved_at=retrieved_at
                    )
                    if not args.no_weights:
                        weights = service.get_weight_history(
                            start=start.isoformat(), end=end.isoformat()
                        )
                        summary.update(repository.ingest_weights(weights, retrieved_at=retrieved_at))
                summary.update(
                    {
                        "status": "ok",
                        "start_date": start.isoformat(),
                        "end_date": end.isoformat(),
                        "database": str(repository.db_path),
                        "unresolved_foods": len(repository.unresolved()),
                    }
                )
                repository.finish_sync(run_id, "ok", summary)
            except Exception as exc:
                repository.finish_sync(run_id, "failed", {"error_type": type(exc).__name__})
                raise
        print(json.dumps(summary, indent=2))
        return 0
    except Exception as exc:  # noqa: BLE001 - CLI boundary; message is credential-free
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
