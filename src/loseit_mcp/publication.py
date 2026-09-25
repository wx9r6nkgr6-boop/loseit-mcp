"""Sanitized, read-only iCloud snapshot publication."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import tempfile
from datetime import date
from pathlib import Path
from uuid import uuid4

from .analytics import period_dates, read_analytics
from .proposals import read_settings, reader
from .repository import NutritionRepository, _now
from .resolved import library_status

SNAPSHOT_SCHEMA_VERSION = 1
HTML_NAME = "nutrition_dashboard.html"
JSON_NAME = "nutrition_snapshot.json"
ICLOUD_ROOT = Path.home() / "Library/Mobile Documents/com~apple~CloudDocs"
FORBIDDEN_EXPORT_MARKERS = (
    "liauth",
    "api_key",
    "raw_diary_snapshots",
    "research_attempts",
)


def default_destination() -> Path | None:
    return ICLOUD_ROOT / "Nutrition Dashboard" if ICLOUD_ROOT.is_dir() else None


def validate_destination(destination: Path) -> Path:
    path = destination.expanduser().resolve()
    root = ICLOUD_ROOT.expanduser().resolve()
    try:
        path.relative_to(root)
    except ValueError:
        raise ValueError("Snapshot destination must be inside this account's iCloud Drive") from None
    return path


def publication_settings(data_dir: Path) -> dict:
    with reader(data_dir) as c:
        exists = c.execute(
            "SELECT 1 FROM sqlite_master WHERE name='publication_settings_versions'"
        ).fetchone()
        row = (
            c.execute(
                "SELECT destination,enabled FROM publication_settings_versions ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if exists
            else None
        )
    default = default_destination()
    return {
        "destination": row[0] if row and row[0] else str(default) if default else None,
        "enabled": bool(row[1]) if row else default is not None,
    }


def save_publication_settings(data_dir: Path, destination: str | Path | None, enabled: bool) -> dict:
    path = validate_destination(Path(destination)) if destination else None
    if enabled and path is None:
        raise ValueError("Choose an iCloud Drive destination before enabling publication")
    with NutritionRepository(Path(data_dir)) as repo, repo.connection as c:
        c.execute(
            "INSERT INTO publication_settings_versions(destination,enabled,created_at) VALUES (?,?,?)",
            (str(path) if path else None, int(enabled), _now()),
        )
    return {"destination": str(path) if path else None, "enabled": bool(enabled)}


def _latest_update_time(data_dir: Path) -> str | None:
    with reader(data_dir) as c:
        row = c.execute(
            "SELECT created_at FROM update_events WHERE stage='finished' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return row[0] if row else None


def build_snapshot(
    data_dir: Path,
    report: dict,
    *,
    generated_at: str | None = None,
    data_updated_at: str | None = None,
) -> dict:
    """Project finished analytics into a deliberately small, stable schema."""
    generated = generated_at or _now()
    nutrient_names = ("calories", "protein_g", "carb_g", "total_fat_g", "fiber_g")
    daily = []
    for row in report["daily_metrics"]:
        daily.append(
            {
                "date": row["date"],
                "occurrences": row["occurrence_count"],
                "source_day_status": row.get("source_day_status", "unknown"),
                "nutrients": {
                    nutrient: {
                        "usable": row["nutrients"][nutrient][2],
                        "missing_occurrences": row["nutrients"][nutrient][5],
                    }
                    for nutrient in nutrient_names
                },
            }
        )
    foods = []
    for food in report["food_contributors"]["catalog"][:30]:
        foods.append(
            {
                "name": food["food_name"],
                "brand": food["brand"],
                "occurrences": food["occurrence_count"],
                "calories": food["metrics"]["calories"][2],
                "protein_g": food["metrics"]["protein_g"][2],
            }
        )
    library = library_status(data_dir)
    return {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "read_only": True,
        "generated_at": generated,
        "nutrition_data_updated_at": data_updated_at or _latest_update_time(data_dir),
        "period": report["period"],
        "insights_period": report["insights_period"],
        "completion": report["completion"],
        "logging_completeness": report["logging_completeness"],
        "period_averages": {
            nutrient: {
                "usable_per_logged_day": report["period_averages"][nutrient][
                    "average_per_logged_day"
                ],
                "coverage_pct": report["period_averages"][nutrient][
                    "combined_coverage_pct"
                ],
            }
            for nutrient in nutrient_names
        },
        "daily": daily,
        "meals": report["meal_summary"],
        "foods": foods,
        "weight": report["weight_summary"],
        "insights": report["insights"],
        "data_quality": {
            "research_items": report["data_quality"]["research_queue_count_global"],
            "source_gap_occurrences": report["data_quality"]["source_gap_occurrences"],
            "filled_occurrences": report["data_quality"][
                "filled_by_scaled_enrichment_occurrences"
            ],
            "resolved_library_foods": library["canonical_foods"],
            "formulations": library["formulations"],
            "active_anomalies": library["active_anomalies"],
            "invalid_source_nutrient_fields": library["invalid_source_nutrient_fields"],
            "last_audits": {
                name: detail["completed_at"] if detail else None
                for name, detail in library["last_audits"].items()
            },
        },
    }


def _display(value, suffix=""):
    return "Unavailable" if value is None else f"{value:,.1f}{suffix}"


def render_html(snapshot: dict) -> str:
    averages = snapshot["period_averages"]
    cards = "".join(
        f'<article class="card"><small>{html.escape(label)}</small><strong>{html.escape(_display(averages[key]["usable_per_logged_day"], suffix))}</strong><span>{html.escape(_display(averages[key]["coverage_pct"], "%"))} coverage</span></article>'
        for key, label, suffix in (
            ("calories", "Calories / logged day", ""),
            ("protein_g", "Protein / logged day", " g"),
            ("carb_g", "Carbohydrates / logged day", " g"),
            ("total_fat_g", "Total fat / logged day", " g"),
            ("fiber_g", "Fiber / logged day", " g"),
        )
    )
    daily_rows = "".join(
        "<tr>"
        f"<td>{html.escape(row['date'])}</td>"
        f"<td>{html.escape(_display(row['nutrients']['calories']['usable']))}</td>"
        f"<td>{html.escape(_display(row['nutrients']['protein_g']['usable'], ' g'))}</td>"
        f"<td>{html.escape(row['source_day_status'].title())}</td>"
        "</tr>"
        for row in snapshot["daily"]
    )
    food_rows = "".join(
        "<tr>"
        f"<td><b>{html.escape(food['name'])}</b><br><span>{html.escape(food['brand'])}</span></td>"
        f"<td>{food['occurrences']}</td><td>{html.escape(_display(food['calories']))}</td>"
        f"<td>{html.escape(_display(food['protein_g'], ' g'))}</td></tr>"
        for food in snapshot["foods"][:15]
    )
    insights = "".join(f"<li>{html.escape(item)}</li>" for item in snapshot["insights"])
    completion = snapshot["completion"]
    completed = completion["latest_completed_date"] or "Not exposed by Lose It"
    updated = snapshot["nutrition_data_updated_at"] or snapshot["generated_at"]
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Nutrition Dashboard</title><style>
:root{{--bg:#101014;--surface:#181820;--raised:#22222d;--text:#f4f4f6;--muted:#a6a6b4;--pink:#ff4f9a;--cyan:#48dfea;--border:#343442;--gap:clamp(.65rem,1.8vw,1.2rem)}}
*{{box-sizing:border-box}}body{{margin:0;background:radial-gradient(ellipse at top right,#ff4f9a18,transparent 48%),var(--bg);color:var(--text);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;font-size:clamp(14px,1.15vw,17px)}}
main{{width:min(100% - clamp(1rem,5vw,4rem),1100px);margin:auto;padding:clamp(1rem,4vw,3rem) 0 4rem}}h1,h2{{text-transform:uppercase;letter-spacing:.04em;margin:.35em 0}}h1{{font-size:clamp(1.65rem,5vw,3rem)}}h2{{font-size:clamp(1rem,2.4vw,1.45rem);margin-top:2rem}}.eyebrow,small{{color:var(--cyan);font-weight:800;text-transform:uppercase;letter-spacing:.07em}}.fresh{{color:var(--muted);display:flex;flex-wrap:wrap;gap:.35rem 1.2rem;margin:1rem 0 1.5rem}}.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,12rem),1fr));gap:var(--gap)}}.card{{min-width:0;padding:clamp(.8rem,2vw,1.15rem);background:var(--raised);border:1px solid var(--border);border-top:2px solid var(--pink);border-radius:12px;display:flex;flex-direction:column;gap:.4rem}}.card strong{{font-size:clamp(1.35rem,3vw,2rem);color:var(--cyan);overflow-wrap:anywhere}}.card span,td span{{color:var(--muted)}}.panel{{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:clamp(.7rem,2vw,1.2rem);overflow-x:auto}}table{{width:100%;border-collapse:collapse;min-width:32rem}}th,td{{padding:.7rem;text-align:left;border-bottom:1px solid var(--border);vertical-align:top}}th{{color:var(--cyan);font-size:.78rem;text-transform:uppercase}}td:first-child{{overflow-wrap:anywhere;max-width:24rem}}li{{margin:.65rem 0;line-height:1.45}}footer{{color:var(--muted);margin-top:2rem;font-size:.85rem}}@media(max-width:520px){{main{{width:min(100% - 1rem,1100px)}}table{{min-width:28rem}}.panel{{padding:.35rem}}}}
</style></head><body><main><div class="eyebrow">Workout companion · read-only nutrition</div><h1>Nutrition snapshot</h1>
<div class="fresh"><span>Nutrition data updated: {html.escape(updated)}</span><span>Insights through: {html.escape(snapshot['insights_period']['end'])}</span><span>Last completed day in Lose It: {html.escape(completed)}</span></div>
<section class="grid">{cards}</section><h2>Useful observations</h2><section class="panel"><ul>{insights}</ul></section>
<h2>Recent days</h2><section class="panel"><table><thead><tr><th>Date</th><th>Calories</th><th>Protein</th><th>Source day state</th></tr></thead><tbody>{daily_rows}</tbody></table></section>
<h2>Frequent foods</h2><section class="panel"><table><thead><tr><th>Food</th><th>Occurrences</th><th>Calories</th><th>Protein</th></tr></thead><tbody>{food_rows}</tbody></table></section>
<footer>Generated {html.escape(snapshot['generated_at'])}. This is a read-only local analytics export. Missing values are never treated as zero.</footer></main></body></html>"""


def _encoded(snapshot: dict) -> tuple[bytes, bytes]:
    json_bytes = (json.dumps(snapshot, ensure_ascii=False, allow_nan=False, indent=2) + "\n").encode()
    html_bytes = render_html(snapshot).encode()
    lowered = (json_bytes + html_bytes).lower()
    if any(marker.encode() in lowered for marker in FORBIDDEN_EXPORT_MARKERS):
        raise ValueError("Snapshot sanitization check failed")
    return html_bytes, json_bytes


def _atomic_pair(destination: Path, html_bytes: bytes, json_bytes: bytes) -> tuple[Path, Path]:
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    staged = []
    backups = []
    installed = []
    try:
        for name, content in ((HTML_NAME, html_bytes), (JSON_NAME, json_bytes)):
            fd, temp = tempfile.mkstemp(prefix=f".{name}.", dir=destination)
            with os.fdopen(fd, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            staged.append((Path(temp), destination / name))
        for _, target in staged:
            if target.exists():
                fd, backup = tempfile.mkstemp(prefix=f".{target.name}.backup.", dir=destination)
                os.close(fd)
                Path(backup).unlink()
                os.replace(target, backup)
                backups.append((Path(backup), target))
        for temp, target in staged:
            os.replace(temp, target)
            installed.append(target)
        return destination / HTML_NAME, destination / JSON_NAME
    except Exception:
        for target in installed:
            target.unlink(missing_ok=True)
        for backup, target in backups:
            if backup.exists():
                os.replace(backup, target)
        raise
    finally:
        for temp, _ in staged:
            temp.unlink(missing_ok=True)
        for backup, _ in backups:
            backup.unlink(missing_ok=True)


def publish_snapshot(
    data_dir: Path,
    *,
    run_id: str | None = None,
    today: date | None = None,
    data_updated_at: str | None = None,
) -> dict:
    settings = publication_settings(data_dir)
    run = run_id or str(uuid4())
    if not settings["enabled"] or not settings["destination"]:
        return {"status": "not_configured", "published_at": None}
    destination = validate_destination(Path(settings["destination"]))
    today = today or date.today()  # noqa: DTZ011
    start, end = period_dates(period="last30", today=today)
    targets = read_settings(data_dir)
    report = read_analytics(
        data_dir,
        start,
        end,
        period="last30",
        grouping="id",
        include_all_foods=True,
        **targets,
    )
    try:
        snapshot = build_snapshot(data_dir, report, data_updated_at=data_updated_at)
        html_bytes, json_bytes = _encoded(snapshot)
        html_path, json_path = _atomic_pair(destination, html_bytes, json_bytes)
        summary = {
            "status": "published",
            "published_at": snapshot["generated_at"],
            "html_path": str(html_path),
            "json_path": str(json_path),
            "html_sha256": hashlib.sha256(html_bytes).hexdigest(),
            "json_sha256": hashlib.sha256(json_bytes).hexdigest(),
        }
    except Exception:  # noqa: BLE001 - privacy boundary; never persist export-bearing errors
        summary = {"status": "failed", "published_at": None}
    with NutritionRepository(Path(data_dir)) as repo, repo.connection as c:
        c.execute(
            """INSERT INTO publication_events
               (run_id,status,html_path,json_path,summary_json,created_at) VALUES (?,?,?,?,?,?)""",
            (
                run,
                summary["status"],
                summary.get("html_path"),
                summary.get("json_path"),
                json.dumps(summary, sort_keys=True),
                _now(),
            ),
        )
    return summary


def publication_status(data_dir: Path) -> dict:
    settings = publication_settings(data_dir)
    with reader(data_dir) as c:
        exists = c.execute(
            "SELECT 1 FROM sqlite_master WHERE name='publication_events'"
        ).fetchone()
        row = (
            c.execute(
                "SELECT status,html_path,json_path,summary_json,created_at FROM publication_events ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if exists
            else None
        )
    latest = json.loads(row[3]) | {"recorded_at": row[4]} if row else None
    return settings | {"latest": latest}


def main(argv=None):
    from .sync_cli import default_data_dir

    parser = argparse.ArgumentParser(description="Publish a sanitized read-only iCloud snapshot")
    parser.add_argument("--data-dir", type=Path, default=default_data_dir())
    args = parser.parse_args(argv)
    result = publish_snapshot(args.data_dir)
    print(json.dumps(result, allow_nan=False))
    return 0 if result["status"] in {"published", "not_configured"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
