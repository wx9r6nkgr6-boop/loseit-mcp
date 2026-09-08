"""Sequential, checkpointed read-only remote ingestion. No remote mutations."""

from __future__ import annotations

import fcntl
import json
import math
import time
from collections import Counter
from datetime import date, timedelta
from typing import Any

from .repository import NutritionRepository, _now


def year_chunks(year: int, today: date | None = None) -> list[tuple[str, str]]:
    today = today or date.today()  # noqa: DTZ011 - intentionally local calendar date
    if year < 1970 or year > today.year:
        raise ValueError("Year must be between 1970 and the current local year")
    start, end = date(year, 1, 1), min(date(year, 12, 31), today)
    result = []
    while start <= end:
        stop = min(start + timedelta(days=30), end)
        result.append((start.isoformat(), stop.isoformat()))
        start = stop + timedelta(days=1)
    return result


def backfill(
    repo: NutritionRepository,
    service: Any,
    year: int,
    *,
    today: date | None = None,
    request_delay: float = 1.0,
    chunk_delay: float = 2.0,
    sleep=time.sleep,
    progress=lambda text: None,
) -> dict:
    """Resume an incomplete pass; a rerun after completion refreshes the whole year."""
    if not all(math.isfinite(v) and v >= 0.25 for v in (request_delay, chunk_delay)):
        raise ValueError("Pacing delays must be finite and at least 0.25 seconds")
    chunks = year_chunks(year, today)
    # OS lock releases on process exit/restart. Checkpoints, not locks, carry progress.
    with (repo.data_dir / "backfill.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Another backfill is running for this repository") from None
        return _run(repo, service, year, chunks, request_delay, chunk_delay, sleep, progress)


def _run(repo, service, year, chunks, request_delay, chunk_delay, sleep, progress):
    c = repo.connection
    run = c.execute(
        "SELECT * FROM backfill_runs WHERE year=? AND status!='complete' ORDER BY id DESC LIMIT 1",
        (year,),
    ).fetchone()
    with c:
        if run:
            run_id = run["id"]
            c.execute(
                "UPDATE backfill_runs SET status='running',end_date=? WHERE id=?",
                (chunks[-1][1], run_id),
            )
        else:
            run_id = c.execute(
                "INSERT INTO backfill_runs(year,end_date,status,started_at) VALUES (?,?,'running',?)",
                (year, chunks[-1][1], _now()),
            ).lastrowid
    result = {
        "requested_year": year,
        "start": chunks[0][0],
        "end": chunks[-1][1],
        "run_id": run_id,
        "date_chunks": len(chunks),
        "successful_chunks": 0,
        "skipped_chunks": 0,
        "failed_chunks": 0,
        "status": "complete",
        "database": str(repo.db_path),
        "request_delay_seconds": request_delay,
        "chunk_delay_seconds": chunk_delay,
    }
    totals: Counter = Counter()
    for index, (start, end) in enumerate(chunks):
        row = c.execute(
            "SELECT * FROM backfill_chunks WHERE run_id=? AND start_date=? AND end_date=?",
            (run_id, start, end),
        ).fetchone()
        if row and row["status"] == "complete":
            result["skipped_chunks"] += 1
            totals.update(json.loads(row["summary_json"]))
            progress(f"Chunk {index + 1}/{len(chunks)} {start}..{end}: already complete")
            continue
        with c:
            c.execute(
                "INSERT INTO backfill_chunks VALUES (?,?,?,'running',NULL) ON CONFLICT(run_id,start_date,end_date) DO UPDATE SET status='running'",
                (run_id, start, end),
            )
        try:
            progress(f"Chunk {index + 1}/{len(chunks)} {start}..{end}: reading")
            sleep(chunk_delay)
            payload = service.get_diary_range(start, end, request_delay=request_delay)
            expected = {
                (date.fromisoformat(start) + timedelta(days=i)).isoformat()
                for i in range((date.fromisoformat(end) - date.fromisoformat(start)).days + 1)
            }
            days = payload.get("days") or []
            if len(days) != len(expected) or {d.get("date") for d in days} != expected:
                raise ValueError(
                    "Incomplete or out-of-range diary response; checkpoint not completed"
                )
            sleep(request_delay)
            weights = service.get_weight_history(start=start, end=end)
            if any(not start <= str(w.get("date", "")) <= end for w in weights.get("entries", [])):
                raise ValueError("Out-of-range weight response")
            known = {
                r[0]
                for r in c.execute(
                    "SELECT DISTINCT source_date FROM raw_diary_snapshots WHERE source_date BETWEEN ? AND ?",
                    (start, end),
                )
            }
            before = c.execute(
                "SELECT COUNT(*) FROM raw_diary_snapshots WHERE source_date BETWEEN ? AND ?",
                (start, end),
            ).fetchone()[0]
            summary = repo.ingest_range(payload, retrieved_at=_now())
            after = c.execute(
                "SELECT COUNT(*) FROM raw_diary_snapshots WHERE source_date BETWEEN ? AND ?",
                (start, end),
            ).fetchone()[0]
            summary["changed_raw_snapshots"] = max(0, after - before - len(expected - known))
            summary.update(repo.ingest_weights(weights, retrieved_at=_now()))
            with c:
                c.execute(
                    "UPDATE backfill_chunks SET status='complete',summary_json=? WHERE run_id=? AND start_date=? AND end_date=?",
                    (json.dumps(summary), run_id, start, end),
                )
            totals.update(summary)
            result["successful_chunks"] += 1
            progress(
                f"Chunk {index + 1}/{len(chunks)}: saved {summary['occurrences_seen']} occurrences"
            )
        except (Exception, KeyboardInterrupt) as exc:  # noqa: BLE001 - durable checkpoint boundary; no exception text logged
            drift = any(
                s in str(exc).lower()
                for s in (
                    "incompatibleremoteservice",
                    "serialization",
                    "policy hash",
                    "permutation",
                )
            )
            result.update(
                status="interrupted" if isinstance(exc, KeyboardInterrupt) else "failed",
                failed_chunks=1,
                error_type=type(exc).__name__,
                next_action="Run uv run loseit-mcp compatibility-check, then rerun backfill"
                if drift
                else "Resolve connection/authentication issue if present, then rerun the same backfill",
            )
            # Never persist exception messages, requests or credentials.
            with c:
                c.execute(
                    "UPDATE backfill_chunks SET status='failed' WHERE run_id=? AND start_date=? AND end_date=?",
                    (run_id, start, end),
                )
            break
    with c:
        c.execute(
            "UPDATE backfill_runs SET status=?,completed_at=? WHERE id=?",
            (result["status"], _now(), run_id),
        )
    result["totals_completed_chunks_including_resumed"] = dict(totals)
    from .analytics import read_analytics

    analytics = read_analytics(
        repo.data_dir,
        date.fromisoformat(chunks[0][0]),
        date.fromisoformat(chunks[-1][1]),
        compare=False,
    )
    result["research_queue_count"] = analytics["data_quality"]["research_queue_count_global"]
    result["standard_nutrient_coverage"] = analytics["nutrient_coverage"]
    return result
