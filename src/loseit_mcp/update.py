"""One explicit update operation for UI and CLI. No background work on import.

Network access is restricted to the injected read-only sync service and optional
research worker. Authentication is loaded only by the live-service factory.
"""

import argparse
import fcntl
import hashlib
import inspect
import json
from contextlib import contextmanager
from datetime import date, timedelta
from pathlib import Path
from uuid import uuid4

from .analytics import read_analytics
from .enrichment_worker import evaluate, make_proposal
from .nutrition_provider import ResearchIssue, build_research_worker
from .proposals import (
    canonical,
    import_proposal,
    list_proposals,
    reader,
    review_proposal,
    source_context,
)
from .reconnect import is_reconnect_error
from .repository import NutritionRepository, _json, _now
from .research_queue import read_research_queue


@contextmanager
def live_service():
    from .config import load_settings
    from .readonly_service import ReadOnlyLoseItService

    with ReadOnlyLoseItService(load_settings()) as service:
        yield service


def sync_window(repo, service, start, end, *, include_weights=True, request_delay=1):
    """Shared validated ingestion path used by both manual sync and one-click updates."""
    run_id = repo.begin_sync(start.isoformat(), end.isoformat())
    try:
        kwargs = {"request_delay": request_delay} if request_delay else {}
        payload = service.get_diary_range(start.isoformat(), end.isoformat(), **kwargs)
        expected = {(start + timedelta(days=i)).isoformat() for i in range((end - start).days + 1)}
        days = payload.get("days", [])
        if len(days) != len(expected) or {d.get("date") for d in days} != expected:
            raise ValueError("Incomplete diary window")
        weights = (
            service.get_weight_history(start=start.isoformat(), end=end.isoformat())
            if include_weights
            else None
        )
        if weights is not None and (
            weights.get("start") != start.isoformat()
            or weights.get("end") != end.isoformat()
            or any(
                not start.isoformat() <= str(w.get("date", "")) <= end.isoformat()
                for w in weights.get("entries", [])
            )
        ):
            raise ValueError("Invalid weight window")
        prior_hashes = {
            row["source_date"]: row["content_sha256"]
            for row in repo.connection.execute(
                """SELECT r.source_date,r.content_sha256 FROM raw_diary_snapshots r
                   JOIN (SELECT source_date,MAX(id) id FROM raw_diary_snapshots
                         WHERE source_date BETWEEN ? AND ? GROUP BY source_date) x ON x.id=r.id""",
                (start.isoformat(), end.isoformat()),
            )
        }
        changed_dates = [
            day["date"]
            for day in days
            if day["date"] in prior_hashes
            and hashlib.sha256(_json(day).encode()).hexdigest() != prior_hashes[day["date"]]
        ]
        summary = repo.ingest_range(payload, retrieved_at=_now())
        summary["changed_dates"] = changed_dates
        if weights is not None:
            summary.update(repo.ingest_weights(weights, retrieved_at=_now()))
        repo.finish_sync(run_id, "ok", summary)
        return summary
    except Exception:
        repo.finish_sync(run_id, "failed", {"error": "Sync incomplete; retry safely"})
        raise


def next_start(data_dir, today):
    # Last fully completed sync, not the maximum diary date: a lone future/historical
    # snapshot must never move a checkpoint. Always replay today plus the prior seven
    # calendar days because Lose It diary dates remain editable after first import.
    with reader(data_dir) as c:
        row = c.execute("SELECT MAX(end_date) FROM sync_runs WHERE status='ok'").fetchone()
        previous = row[0]
        if not previous:
            # A completed historical backfill is also a valid checkpoint.
            row = c.execute(
                "SELECT MAX(end_date) FROM backfill_runs WHERE status='complete'"
            ).fetchone()
            previous = row[0]
        reconciliation_start = today - timedelta(days=7)
        return min(reconciliation_start, date.fromisoformat(previous)) if previous else reconciliation_start


def latest_update(data_dir):
    with reader(data_dir) as c:
        if not c.execute("SELECT 1 FROM sqlite_master WHERE name='update_events'").fetchone():
            return None
        row = c.execute(
            "SELECT summary_json FROM update_events WHERE stage='finished' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return json.loads(row[0]) if row else None


def _event(data_dir, run, stage, summary):
    with NutritionRepository(data_dir) as repo, repo.connection as c:
        c.execute(
            "INSERT INTO update_events(run_id,stage,summary_json,created_at) VALUES (?,?,?,?)",
            (run, stage, canonical(summary), _now()),
        )


def _research_event(data_dir, run, source_food_id, provider, status, summary):
    safe = canonical(summary)
    digest = hashlib.sha256(
        canonical(
            {
                "source_food_id": source_food_id,
                "provider": provider,
                "status": status,
                "summary": summary,
            }
        ).encode()
    ).hexdigest()
    with NutritionRepository(Path(data_dir)) as repo, repo.connection as c:
        c.execute(
            """INSERT OR IGNORE INTO research_attempts
               (run_id,source_food_id,provider,status,summary_json,content_sha256,created_at)
               VALUES (?,?,?,?,?,?,?)""",
            (run, source_food_id, provider, status, safe, digest, _now()),
        )


def latest_research_issues(data_dir):
    """Latest provider exception per food, with no credential or diary payloads."""
    with reader(data_dir) as c:
        if not c.execute("SELECT 1 FROM sqlite_master WHERE name='research_attempts'").fetchone():
            return []
        rows = c.execute(
            """SELECT a.source_food_id,a.provider,a.status,a.summary_json,a.created_at
               FROM research_attempts a
               JOIN (SELECT source_food_id,MAX(id) id FROM research_attempts GROUP BY source_food_id) x
                 ON x.id=a.id
               WHERE a.status IN ('no_exact_match','ambiguous_match','conflicting_evidence',
                                  'serving_mismatch','branded_generic_blocked')
               ORDER BY a.id DESC"""
        ).fetchall()
    return [
        {
            "source_food_id": row[0],
            "provider": row[1],
            "status": row[2],
            "summary": json.loads(row[3]),
            "created_at": row[4],
        }
        for row in rows
    ]


def _apply_evidence(data_dir, candidate, evidence):
    doc = make_proposal(candidate, evidence)
    imported = import_proposal(data_dir, canonical(doc).encode())
    pid = imported["id"]
    # Human rejection/deferral/approval is durable; a routine update must not undo it.
    current = next(p for p in list_proposals(data_dir) if p["id"] == pid)
    if imported["duplicate"] and any(a["action"] != "imported" for a in current["history"]):
        return "existing", 0
    with reader(data_dir) as c:
        decision = evaluate(evidence, doc, source_context(c, evidence.source_food_id))
        flag = c.execute(
            "SELECT status FROM source_review_flags WHERE source_food_id=? ORDER BY id DESC LIMIT 1",
            (evidence.source_food_id,),
        ).fetchone()
    if flag and flag[0] != "cleared":
        decision["auto_approve"] = False
        decision["reasons"].append("Source has a manual review flag.")
    if current["context_problem"] or current["conflicting_proposals"]:
        decision["auto_approve"] = False
        decision["reasons"].append("Conflicting or stale source/proposal context requires review.")
    if any(
        p["document"]["target"]["source_food_id"] == evidence.source_food_id
        and any(a["action"] in {"rejected", "deferred"} for a in p["history"])
        for p in list_proposals(data_dir)
    ):
        decision["auto_approve"] = False
        decision["reasons"].append("A previous human rejection/deferral requires renewed review.")
    if decision["auto_approve"]:
        selected = [n["nutrient"] for n in doc["nutrients"]]
        review_proposal(
            data_dir,
            pid,
            "approve",
            selected=selected,
            note="Automatically accepted by exact-label-v1; no human approval claimed.",
            policy_evidence=evidence,
        )
        return "automatic", len(selected)
    with NutritionRepository(data_dir) as repo, repo.connection as c:
        c.execute(
            "INSERT INTO research_decisions(proposal_id,evidence_json,decision_json,created_at) VALUES (?,?,?,?)",
            (pid, canonical(decision.pop("evidence")), canonical(decision), _now()),
        )
        c.execute(
            "INSERT INTO proposal_reviews(proposal_id,action,note,created_at,actor) VALUES (?,'needs_review',?,?,'policy')",
            (pid, " ".join(decision["reasons"]), _now()),
        )
    return "review", 0


def process_research(data_dir, *, worker=None, run_id=None, progress=lambda text: None):
    """Shared local evidence stage, also callable for an explicit seed migration.

    Never syncs or claims a through-date. The caller must serialize writes.
    """
    worker = worker if worker is not None else build_research_worker()
    run_id = run_id or str(uuid4())
    queue = read_research_queue(data_dir)
    result = {
        "candidates": queue["queue_count"],
        "foods_checked": 0,
        "researched": 0,
        "automatically_enriched": 0,
        "nutrients_filled": 0,
        "sent_to_review": 0,
        "failures": 0,
        "research_unavailable": 0,
        "unresolved": 0,
    }
    # Explicitly supplied exception: annotation only, never fabricated enrichment.
    arnold = "f96a4d4da2c2c98d064aac70c45fd3ee"
    if any(f["source_food_id"] == arnold for f in queue["foods"]):
        with NutritionRepository(Path(data_dir)) as repo, repo.connection as c:
            if not c.execute(
                "SELECT 1 FROM source_review_flags WHERE source_food_id=?", (arnold,)
            ).fetchone():
                c.execute(
                    "INSERT INTO source_review_flags(source_food_id,status,note,created_at) VALUES (?,'needs_review',?,?)",
                    (
                        arnold,
                        "User-reported serving contradiction: most recorded values scale as four slices while sodium scales as two. Confirm actual slice count and label. Do not automatically fill protein.",
                        _now(),
                    ),
                )
    progress("Researching missing nutrition · checking available evidence")
    for candidate in queue["foods"]:
        result["foods_checked"] += 1
        if not candidate["import_target"]:
            result["unresolved"] += 1
            continue
        try:
            keys = ["source_food_id", "food_name", "brand"]
            if getattr(worker, "requires_context", False):
                keys.extend(("portion_variants", "missing_standard_nutrients"))
            request = {key: candidate[key] for key in keys}
            if getattr(worker, "requires_context", False):
                from .review_inbox import latest_answer

                request["human_answer"] = latest_answer(
                    data_dir, candidate["source_food_id"]
                )
            evidence = worker.research(request)
            if evidence is None:
                _research_event(
                    data_dir,
                    run_id,
                    candidate["source_food_id"],
                    getattr(worker, "provider", "configured worker"),
                    "no_result",
                    {"message": "No exact evidence was returned."},
                )
                result["unresolved"] += 1
                continue
            result["researched"] += 1
            _research_event(
                data_dir,
                run_id,
                candidate["source_food_id"],
                getattr(worker, "provider", evidence.source_kind),
                "evidence_found",
                {
                    "title": evidence.title,
                    "reference_url": evidence.url,
                    "research_date": evidence.research_date,
                    "source_kind": evidence.source_kind,
                    "basis": {
                        "amount": evidence.basis_amount,
                        "unit": evidence.basis_unit,
                        "description": evidence.basis_description,
                    },
                },
            )
            progress("Applying verified enrichment · checking identity, portion and fingerprint")
            outcome, count = _apply_evidence(data_dir, candidate, evidence)
            result["automatically_enriched"] += outcome == "automatic"
            result["sent_to_review"] += outcome == "review"
            result["nutrients_filled"] += count
        except ResearchIssue as exc:
            _research_event(
                data_dir,
                run_id,
                candidate["source_food_id"],
                exc.provider,
                exc.status,
                {"message": exc.summary},
            )
            result["sent_to_review"] += int(exc.needs_review)
            result["failures"] += int(exc.failure)
            result["research_unavailable"] += int(
                exc.status
                in {
                    "provider_not_configured",
                    "provider_unavailable",
                    "timeout",
                    "malformed_response",
                }
            )
            result["unresolved"] += 1
        except Exception:  # noqa: BLE001 - sanitize external worker failures
            result["failures"] += 1
            result["unresolved"] += 1
    return result


def run_update(
    data_dir,
    *,
    service_factory=live_service,
    worker=None,
    today=None,
    progress=lambda text: None,
    trigger="manual",
    publish=False,
):
    data_dir = Path(data_dir).expanduser().resolve()
    if not (data_dir / "nutrition.sqlite3").is_file():
        raise ValueError("Existing nutrition repository required")
    today = today or date.today()  # noqa: DTZ011
    worker = worker if worker is not None else build_research_worker()
    result = {
        "status": "failed",
        "through_date": None,
        "occurrences_added": 0,
        "weights_added": 0,
        "candidates": 0,
        "foods_checked": 0,
        "researched": 0,
        "automatically_enriched": 0,
        "nutrients_filled": 0,
        "sent_to_review": 0,
        "failures": 0,
        "research_unavailable": 0,
        "unresolved": 0,
        "analytics_refreshed": False,
        "trigger": trigger,
        "reconciliation_start": None,
        "recent_dates_rechecked": 0,
        "changed_recent_days": 0,
        "occurrences_removed": 0,
        "occurrences_changed": 0,
        "static_snapshot": {"status": "not_requested", "published_at": None},
        "research_capability": getattr(worker, "capability", "Configured worker"),
    }
    run = str(uuid4())
    started = False
    # Same lock as backfill prevents simultaneous history refreshes. flock is released
    # by the OS after a crash; append-only events retain unfinished stages.
    with (data_dir / "backfill.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return result | {"error": "Another update or backfill is running."}
        try:
            progress("Checking Lose It authentication and compatibility")
            try:
                with service_factory() as service:
                    # No identity or auth data is saved, returned or logged.
                    service.whoami()
                    service.search_food("water", limit=1, detail=False)
                    _event(data_dir, run, "auth_checked", {"status": "connected"})
                    before = read_analytics(
                        data_dir, date(today.year, 1, 1), today, compare=False
                    )
                    result["coverage_before"] = before["nutrient_coverage"]
                    start = next_start(data_dir, today)
                    result["reconciliation_start"] = max(
                        start, today - timedelta(days=7)
                    ).isoformat()
                    _event(data_dir, run, "started", {"requested_through": today.isoformat()})
                    started = True
                    while start <= today:
                        end = min(start + timedelta(days=30), today)
                        progress(f"Syncing Lose It · {start} through {end}")
                        with NutritionRepository(data_dir) as repo:
                            summary = sync_window(repo, service, start, end)
                        for key in (
                            "occurrences_added",
                            "weights_added",
                            "occurrences_removed",
                            "occurrences_changed",
                        ):
                            result[key] += summary.get(key, 0)
                        result["changed_recent_days"] += sum(
                            changed >= today - timedelta(days=7)
                            for changed in map(date.fromisoformat, summary["changed_dates"])
                        )
                        result["through_date"] = end.isoformat()
                        _event(data_dir, run, "sync_checkpoint", result)
                        start = end + timedelta(days=1)
            except Exception as exc:
                if not is_reconnect_error(exc):
                    raise
                result["status"] = "reconnect_required"
                result["error"] = "Lose It connection expired. Reconnect to continue this update."
                _event(data_dir, run, "reconnect_required", {"status": "reconnect_required"})
                return result
            progress("Checking nutrition coverage")
            result.update(
                process_research(data_dir, worker=worker, run_id=run, progress=progress)
            )
            progress("Updating analytics")
            report = read_analytics(data_dir, date(today.year, 1, 1), today, compare=False)
            result["coverage_after"] = report["nutrient_coverage"]
            result["remaining_research_queue"] = report["data_quality"][
                "research_queue_count_global"
            ]
            proposals = list_proposals(data_dir)
            review_ids = {
                p["document"]["target"]["source_food_id"]
                for p in proposals
                if p["status"] in {"needs_review", "ready", "partially_approved"}
            }
            review_ids |= {
                f["source_food_id"] for f in report["data_quality"]["foods"] if f["review_flags"]
            }
            review_ids |= {row["source_food_id"] for row in latest_research_issues(data_dir)}
            result["needs_review"] = len(review_ids)
            result["analytics_refreshed"] = True
            result["recent_dates_rechecked"] = 8
            result["status"] = (
                "complete_with_unresolved"
                if result["unresolved"] or result["needs_review"]
                else "complete"
            )
            with reader(data_dir) as c:
                result["integrity"] = c.execute("PRAGMA integrity_check").fetchone()[0]
            if publish:
                progress("Publishing read-only iCloud snapshot")
                from .publication import publish_snapshot

                result["static_snapshot"] = publish_snapshot(
                    data_dir, run_id=run, today=today, data_updated_at=_now()
                )
            _event(data_dir, run, "finished", result)
            return result
        except Exception:  # noqa: BLE001 - redact failures at UI/CLI boundary
            result["error"] = (
                "Update stopped. Check authentication/compatibility and retry; prior checkpoints are retained."
            )
            # Preflight failures must not migrate/write the repository.
            if started:
                _event(data_dir, run, "finished", result)
            return result


def main(argv=None):
    from .sync_cli import default_data_dir

    parser = argparse.ArgumentParser(
        description="Explicit read-only Lose It sync and controlled local enrichment"
    )
    parser.add_argument("--data-dir", type=Path, default=default_data_dir())
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    options = {"publish": True} if "publish" in inspect.signature(run_update).parameters else {}
    result = run_update(args.data_dir, **options)
    if args.json:
        print(json.dumps(result, allow_nan=False))
    else:
        print(f"Update: {result['status']} · through {result['through_date'] or 'not advanced'}")
        print(
            f"{result['occurrences_added']} entries, {result['weights_added']} weights added; "
            f"{result['automatically_enriched']} foods enriched, {result['nutrients_filled']} nutrients filled."
        )
        print(
            f"Needs review: {result.get('needs_review', 'not calculated')}; unresolved: {result['unresolved']}."
        )
        print(result.get("error") or result["research_capability"])
    return 3 if result["status"] == "reconnect_required" else 2 if result["status"] == "failed" else 0
