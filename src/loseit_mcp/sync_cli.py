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
    parser.add_argument("--json", action="store_true", help="Emit coverage as JSON.")
    parser.add_argument("--missing-only", action="store_true", help="Only show foods with coverage gaps.")
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
        end = datetime.now(UTC).date()
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
        if (args.json or args.missing_only) and not args.coverage:
            raise ValueError("--json and --missing-only require --coverage")
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
