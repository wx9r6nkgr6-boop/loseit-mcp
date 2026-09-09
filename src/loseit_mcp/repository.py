"""Durable, local-only SQLite repository for nutrition analysis."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import unicodedata
from contextlib import nullcontext
from datetime import UTC, datetime
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Self

SCHEMA_VERSION = 4
SOURCE = "loseit"
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
MATCH_TYPES = frozenset(
    {
        "exact_brand_product",
        "same_brand_close_product",
        "USDA_or_other_authoritative_generic",
        "reputable_manufacturer_equivalent",
        "generic_food_average",
        "calorie_constrained_inference",
        "insufficient_information",
    }
)
PROVENANCE_TYPES = frozenset(
    {
        "researched_exact_product",
        "researched_brand_equivalent",
        "researched_generic_food",
        "inferred_from_calories",
    }
)

MIGRATIONS = (
    """
    CREATE TABLE IF NOT EXISTS schema_version (
        version INTEGER PRIMARY KEY,
        applied_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS sync_runs (
        id INTEGER PRIMARY KEY,
        started_at TEXT NOT NULL,
        completed_at TEXT,
        start_date TEXT NOT NULL,
        end_date TEXT NOT NULL,
        status TEXT NOT NULL,
        summary_json TEXT
    );
    CREATE TABLE IF NOT EXISTS raw_diary_snapshots (
        id INTEGER PRIMARY KEY,
        source TEXT NOT NULL,
        source_date TEXT NOT NULL,
        retrieved_at TEXT NOT NULL,
        content_sha256 TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        file_path TEXT,
        UNIQUE(source, source_date, content_sha256)
    );
    CREATE TABLE IF NOT EXISTS raw_weight_snapshots (
        id INTEGER PRIMARY KEY,
        source TEXT NOT NULL,
        start_date TEXT NOT NULL,
        end_date TEXT NOT NULL,
        retrieved_at TEXT NOT NULL,
        content_sha256 TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        file_path TEXT,
        UNIQUE(source, start_date, end_date, content_sha256)
    );
    CREATE TABLE IF NOT EXISTS food_occurrences (
        id INTEGER PRIMARY KEY,
        stable_key TEXT NOT NULL UNIQUE,
        source TEXT NOT NULL,
        source_date TEXT NOT NULL,
        meal TEXT,
        food_name_raw TEXT,
        food_name_normalized TEXT NOT NULL,
        brand_raw TEXT,
        brand_normalized TEXT NOT NULL,
        amount REAL,
        unit TEXT,
        servings REAL,
        source_food_id TEXT,
        source_entry_id TEXT,
        source_logged_at TEXT,
        ingestion_timestamp TEXT NOT NULL,
        raw_snapshot_id INTEGER NOT NULL REFERENCES raw_diary_snapshots(id)
    );
    CREATE INDEX IF NOT EXISTS idx_occurrences_date ON food_occurrences(source_date);
    CREATE INDEX IF NOT EXISTS idx_occurrences_food
        ON food_occurrences(food_name_normalized, brand_normalized);
    CREATE INDEX IF NOT EXISTS idx_occurrences_source_food ON food_occurrences(source_food_id);
    CREATE TABLE IF NOT EXISTS nutrient_observations (
        id INTEGER PRIMARY KEY,
        occurrence_id INTEGER NOT NULL REFERENCES food_occurrences(id) ON DELETE RESTRICT,
        nutrient TEXT NOT NULL,
        value REAL NOT NULL,
        unit TEXT NOT NULL,
        provenance TEXT NOT NULL CHECK (provenance = 'loseit'),
        UNIQUE(occurrence_id, nutrient, provenance)
    );
    CREATE INDEX IF NOT EXISTS idx_nutrients_name ON nutrient_observations(nutrient);
    CREATE TABLE IF NOT EXISTS food_references (
        id INTEGER PRIMARY KEY,
        food_name TEXT NOT NULL,
        brand TEXT,
        food_name_normalized TEXT NOT NULL,
        brand_normalized TEXT NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE(food_name_normalized, brand_normalized)
    );
    CREATE TABLE IF NOT EXISTS food_aliases (
        id INTEGER PRIMARY KEY,
        reference_id INTEGER NOT NULL REFERENCES food_references(id) ON DELETE RESTRICT,
        alias_name_normalized TEXT NOT NULL,
        alias_brand_normalized TEXT NOT NULL,
        manually_confirmed INTEGER NOT NULL CHECK (manually_confirmed IN (0, 1)),
        created_at TEXT NOT NULL,
        UNIQUE(alias_name_normalized, alias_brand_normalized)
    );
    CREATE TABLE IF NOT EXISTS enrichment_versions (
        id INTEGER PRIMARY KEY,
        reference_id INTEGER NOT NULL REFERENCES food_references(id) ON DELETE RESTRICT,
        version INTEGER NOT NULL,
        source_reference TEXT NOT NULL,
        reference_url TEXT,
        match_type TEXT NOT NULL,
        confidence TEXT NOT NULL CHECK (confidence IN ('low', 'medium', 'high')),
        assumptions TEXT NOT NULL,
        research_date TEXT NOT NULL,
        manually_reviewed INTEGER NOT NULL CHECK (manually_reviewed IN (0, 1)),
        created_at TEXT NOT NULL,
        UNIQUE(reference_id, version)
    );
    CREATE TABLE IF NOT EXISTS estimated_nutrients (
        id INTEGER PRIMARY KEY,
        enrichment_version_id INTEGER NOT NULL
            REFERENCES enrichment_versions(id) ON DELETE RESTRICT,
        nutrient TEXT NOT NULL,
        estimated_value REAL,
        lower_bound REAL,
        upper_bound REAL,
        unit TEXT NOT NULL,
        provenance TEXT NOT NULL,
        CHECK (estimated_value IS NOT NULL OR (lower_bound IS NOT NULL AND upper_bound IS NOT NULL)),
        CHECK (lower_bound IS NULL OR upper_bound IS NULL OR lower_bound <= upper_bound),
        UNIQUE(enrichment_version_id, nutrient)
    );
    CREATE TABLE IF NOT EXISTS occurrence_reference_links (
        occurrence_id INTEGER PRIMARY KEY REFERENCES food_occurrences(id) ON DELETE RESTRICT,
        reference_id INTEGER NOT NULL REFERENCES food_references(id) ON DELETE RESTRICT,
        match_method TEXT NOT NULL,
        confidence TEXT NOT NULL,
        linked_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS enrichment_queue (
        id INTEGER PRIMARY KEY,
        food_name_normalized TEXT NOT NULL,
        brand_normalized TEXT NOT NULL,
        source_food_id TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'unresolved',
        reason TEXT NOT NULL,
        fuzzy_suggestions_json TEXT NOT NULL DEFAULT '[]',
        occurrence_count INTEGER NOT NULL DEFAULT 1,
        first_seen_at TEXT NOT NULL,
        last_seen_at TEXT NOT NULL,
        UNIQUE(food_name_normalized, brand_normalized, source_food_id)
    );
    CREATE TABLE IF NOT EXISTS weight_observations (
        id INTEGER PRIMARY KEY,
        source TEXT NOT NULL,
        source_date TEXT NOT NULL,
        weight REAL NOT NULL,
        unit TEXT,
        retrieved_at TEXT NOT NULL,
        UNIQUE(source, source_date)
    );
    CREATE VIEW IF NOT EXISTS normalized_food_records AS
    SELECT
        o.id, o.source_date AS date, o.meal, o.source,
        o.food_name_raw, o.food_name_normalized,
        o.brand_raw, o.brand_normalized, o.amount, o.unit, o.servings,
        MAX(CASE WHEN n.nutrient='calories' THEN n.value END) AS calories,
        MAX(CASE WHEN n.nutrient='protein_g' THEN n.value END) AS protein_g,
        MAX(CASE WHEN n.nutrient='carb_g' THEN n.value END) AS carbohydrate_g,
        MAX(CASE WHEN n.nutrient='total_fat_g' THEN n.value END) AS total_fat_g,
        MAX(CASE WHEN n.nutrient='saturated_fat_g' THEN n.value END) AS saturated_fat_g,
        MAX(CASE WHEN n.nutrient='fiber_g' THEN n.value END) AS fiber_g,
        MAX(CASE WHEN n.nutrient='sugar_g' THEN n.value END) AS sugar_g,
        MAX(CASE WHEN n.nutrient='sodium_mg' THEN n.value END) AS sodium_mg,
        MAX(CASE WHEN n.nutrient='cholesterol_mg' THEN n.value END) AS cholesterol_mg,
        CASE WHEN MAX(CASE WHEN n.nutrient='calories' THEN 1 END)=1 THEN 'loseit' ELSE 'unknown' END AS calories_provenance,
        CASE WHEN MAX(CASE WHEN n.nutrient='protein_g' THEN 1 END)=1 THEN 'loseit' ELSE 'unknown' END AS protein_g_provenance,
        CASE WHEN MAX(CASE WHEN n.nutrient='carb_g' THEN 1 END)=1 THEN 'loseit' ELSE 'unknown' END AS carbohydrate_g_provenance,
        CASE WHEN MAX(CASE WHEN n.nutrient='total_fat_g' THEN 1 END)=1 THEN 'loseit' ELSE 'unknown' END AS total_fat_g_provenance,
        CASE WHEN MAX(CASE WHEN n.nutrient='saturated_fat_g' THEN 1 END)=1 THEN 'loseit' ELSE 'unknown' END AS saturated_fat_g_provenance,
        CASE WHEN MAX(CASE WHEN n.nutrient='fiber_g' THEN 1 END)=1 THEN 'loseit' ELSE 'unknown' END AS fiber_g_provenance,
        CASE WHEN MAX(CASE WHEN n.nutrient='sugar_g' THEN 1 END)=1 THEN 'loseit' ELSE 'unknown' END AS sugar_g_provenance,
        CASE WHEN MAX(CASE WHEN n.nutrient='sodium_mg' THEN 1 END)=1 THEN 'loseit' ELSE 'unknown' END AS sodium_mg_provenance,
        CASE WHEN MAX(CASE WHEN n.nutrient='cholesterol_mg' THEN 1 END)=1 THEN 'loseit' ELSE 'unknown' END AS cholesterol_mg_provenance,
        o.source_food_id, o.source_entry_id, o.ingestion_timestamp
    FROM food_occurrences o
    LEFT JOIN nutrient_observations n ON n.occurrence_id=o.id
    GROUP BY o.id;
    CREATE VIEW IF NOT EXISTS latest_enrichment_versions AS
    SELECT ev.* FROM enrichment_versions ev
    JOIN (
        SELECT reference_id, MAX(version) AS version
        FROM enrichment_versions GROUP BY reference_id
    ) latest ON latest.reference_id=ev.reference_id AND latest.version=ev.version;
    """,
    """
    CREATE TABLE source_reference_targets (
        source TEXT NOT NULL,
        source_food_id TEXT NOT NULL,
        food_name_normalized TEXT NOT NULL,
        brand_normalized TEXT NOT NULL,
        reference_id INTEGER NOT NULL REFERENCES food_references(id),
        created_at TEXT NOT NULL,
        PRIMARY KEY(source, source_food_id, food_name_normalized, brand_normalized)
    );
    CREATE TABLE enrichment_review_contexts (
        enrichment_version_id INTEGER PRIMARY KEY REFERENCES enrichment_versions(id),
        source_context_json TEXT NOT NULL,
        nutrition_basis_json TEXT NOT NULL
    );
    """,
    """
    ALTER TABLE food_occurrences ADD COLUMN is_current INTEGER NOT NULL DEFAULT 1;
    CREATE TABLE backfill_runs (
        id INTEGER PRIMARY KEY, year INTEGER NOT NULL, end_date TEXT NOT NULL,
        status TEXT NOT NULL, started_at TEXT NOT NULL, completed_at TEXT
    );
    CREATE TABLE backfill_chunks (
        run_id INTEGER NOT NULL REFERENCES backfill_runs(id), start_date TEXT NOT NULL,
        end_date TEXT NOT NULL, status TEXT NOT NULL, summary_json TEXT,
        PRIMARY KEY(run_id,start_date,end_date)
    );
    CREATE VIEW analytics_source_values AS
    SELECT o.id AS occurrence_id,o.source_date,o.meal,o.source_food_id,
           n.nutrient,n.value,n.unit FROM food_occurrences o
    JOIN nutrient_observations n ON n.occurrence_id=o.id WHERE o.is_current=1;
    CREATE VIEW analytics_daily_source AS
    SELECT source_date,nutrient,SUM(value) AS known_total,COUNT(*) AS present_count
    FROM analytics_source_values GROUP BY source_date,nutrient;
    """,
    """
    CREATE TABLE research_proposals (
        id INTEGER PRIMARY KEY, content_sha256 TEXT NOT NULL UNIQUE,
        source_food_id TEXT NOT NULL, document_json TEXT NOT NULL, created_at TEXT NOT NULL
    );
    CREATE INDEX idx_proposals_food ON research_proposals(source_food_id);
    CREATE TABLE proposal_reviews (
        id INTEGER PRIMARY KEY, proposal_id INTEGER NOT NULL REFERENCES research_proposals(id),
        action TEXT NOT NULL, document_json TEXT, selected_json TEXT NOT NULL DEFAULT '[]',
        user_edited INTEGER NOT NULL DEFAULT 0, enrichment_version_id INTEGER REFERENCES enrichment_versions(id),
        note TEXT NOT NULL, created_at TEXT NOT NULL
    );
    CREATE INDEX idx_reviews_proposal ON proposal_reviews(proposal_id,id);
    CREATE TABLE source_review_flags (
        id INTEGER PRIMARY KEY, source_food_id TEXT NOT NULL, status TEXT NOT NULL,
        note TEXT NOT NULL, created_at TEXT NOT NULL
    );
    CREATE INDEX idx_source_review_food ON source_review_flags(source_food_id,id);
    CREATE TABLE dashboard_settings_versions (
        id INTEGER PRIMARY KEY, settings_json TEXT NOT NULL, created_at TEXT NOT NULL
    );
    CREATE TRIGGER proposals_no_update BEFORE UPDATE ON research_proposals BEGIN SELECT RAISE(ABORT,'Proposal history is append-only'); END;
    CREATE TRIGGER proposals_no_delete BEFORE DELETE ON research_proposals BEGIN SELECT RAISE(ABORT,'Proposal history is append-only'); END;
    CREATE TRIGGER reviews_no_update BEFORE UPDATE ON proposal_reviews BEGIN SELECT RAISE(ABORT,'Review history is append-only'); END;
    CREATE TRIGGER reviews_no_delete BEFORE DELETE ON proposal_reviews BEGIN SELECT RAISE(ABORT,'Review history is append-only'); END;
    CREATE TRIGGER flags_no_update BEFORE UPDATE ON source_review_flags BEGIN SELECT RAISE(ABORT,'Flag history is append-only'); END;
    CREATE TRIGGER flags_no_delete BEFORE DELETE ON source_review_flags BEGIN SELECT RAISE(ABORT,'Flag history is append-only'); END;
    CREATE TRIGGER settings_no_update BEFORE UPDATE ON dashboard_settings_versions BEGIN SELECT RAISE(ABORT,'Settings history is append-only'); END;
    CREATE TRIGGER settings_no_delete BEFORE DELETE ON dashboard_settings_versions BEGIN SELECT RAISE(ABORT,'Settings history is append-only'); END;
    """,
)


class NutritionRepository:
    """Own the canonical database and append-only raw snapshot files."""

    def __init__(self, data_dir: Path):
        self.data_dir = data_dir.expanduser().resolve()
        self.db_path = self.data_dir / "nutrition.sqlite3"
        self.raw_dir = self.data_dir / "raw"
        self.normalized_dir = self.data_dir / "normalized"
        self.enriched_dir = self.data_dir / "enriched"
        self.reference_dir = self.data_dir / "reference"
        self.reports_dir = self.data_dir / "reports"
        for directory in (
            self.raw_dir,
            self.normalized_dir,
            self.enriched_dir,
            self.reference_dir,
            self.reports_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.db_path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA journal_mode = WAL")
        self._migrate()

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def _migrate(self) -> None:
        current = self.connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_version'"
        ).fetchone()
        version = 0
        if current:
            row = self.connection.execute("SELECT MAX(version) AS version FROM schema_version").fetchone()
            version = int(row["version"] or 0)
        for number, sql in enumerate(MIGRATIONS, 1):
            if number <= version:
                continue
            # executescript commits pending transactions itself. Put DDL and the
            # version marker inside its own explicit transaction for crash safety.
            try:
                self.connection.executescript(
                    "BEGIN IMMEDIATE;\n" + sql
                    + f"\nINSERT INTO schema_version(version,applied_at) VALUES ({number},'{_now()}');\nCOMMIT;"
                )
            except Exception:
                self.connection.rollback()
                raise
        if version > SCHEMA_VERSION:
            raise RuntimeError(
                f"Database schema {version} is newer than supported schema {SCHEMA_VERSION}."
            )

    def begin_sync(self, start_date: str, end_date: str) -> int:
        with self.connection:
            cursor = self.connection.execute(
                "INSERT INTO sync_runs(started_at,start_date,end_date,status) VALUES (?,?,?,'running')",
                (_now(), start_date, end_date),
            )
        return int(cursor.lastrowid)

    def finish_sync(self, run_id: int, status: str, summary: dict[str, Any]) -> None:
        with self.connection:
            self.connection.execute(
                "UPDATE sync_runs SET completed_at=?, status=?, summary_json=? WHERE id=?",
                (_now(), status, _json(summary), run_id),
            )

    def ingest_range(self, payload: dict[str, Any], *, retrieved_at: str | None = None) -> dict[str, int]:
        retrieved = retrieved_at or _now()
        summary = {"days": 0, "raw_snapshots_added": 0, "occurrences_added": 0,
                   "occurrences_seen": 0, "enrichments_reused": 0, "queued": 0}
        for day in payload.get("days") or []:
            result = self.ingest_day(day, retrieved_at=retrieved)
            summary["days"] += 1
            for key in summary.keys() - {"days"}:
                summary[key] += result[key]
        return summary

    def ingest_day(self, day: dict[str, Any], *, retrieved_at: str) -> dict[str, int]:
        source_date = str(day["date"])
        serialized = _json(day)
        digest = hashlib.sha256(serialized.encode()).hexdigest()
        raw_file = self._write_raw_file(source_date, retrieved_at, digest, serialized)
        result = {"raw_snapshots_added": 0, "occurrences_added": 0, "occurrences_seen": 0,
                  "enrichments_reused": 0, "queued": 0}
        with self.connection:
            before = self.connection.total_changes
            self.connection.execute(
                """INSERT OR IGNORE INTO raw_diary_snapshots
                   (source,source_date,retrieved_at,content_sha256,payload_json,file_path)
                   VALUES (?,?,?,?,?,?)""",
                (SOURCE, source_date, retrieved_at, digest, serialized, str(raw_file)),
            )
            result["raw_snapshots_added"] = self.connection.total_changes - before
            snapshot = self.connection.execute(
                "SELECT id FROM raw_diary_snapshots WHERE source=? AND source_date=? AND content_sha256=?",
                (SOURCE, source_date, digest),
            ).fetchone()
            assert snapshot is not None
            self.connection.execute(
                "UPDATE food_occurrences SET is_current=0 WHERE source=? AND source_date=?",
                (SOURCE, source_date),
            )
            for position, entry in enumerate(day.get("entries") or []):
                result["occurrences_seen"] += 1
                added = self._ingest_occurrence(
                    source_date, position, entry, int(snapshot["id"]), retrieved_at
                )
                result["occurrences_added"] += added
                self.connection.execute(
                    "UPDATE food_occurrences SET is_current=1 WHERE stable_key=?",
                    (self._stable_key(source_date, position, entry),),
                )
                linked, queued = self._match_or_queue(
                    source_date, position, entry, increment_queue=bool(added)
                )
                result["enrichments_reused"] += linked
                result["queued"] += queued
        return result

    def _write_raw_file(self, source_date: str, retrieved_at: str, digest: str, payload: str) -> Path:
        directory = self.raw_dir / source_date[:4] / source_date[5:7]
        directory.mkdir(parents=True, exist_ok=True)
        stamp = re.sub(r"[^0-9]", "", retrieved_at)[:14]
        path = directory / f"{source_date}T{stamp}-{digest[:12]}.json"
        if not path.exists():
            path.write_text(payload + "\n", encoding="utf-8")
        return path

    def _stable_key(self, source_date: str, position: int, entry: dict[str, Any]) -> str:
        if entry.get("entry_id"):
            return f"{SOURCE}:{source_date}:entry:{entry['entry_id']}"
        stable = {
            "date": source_date,
            "position": position,
            "meal": entry.get("meal"),
            "food_name": entry.get("food_name"),
            "brand": entry.get("food_brand"),
            "amount": entry.get("amount"),
            "unit": entry.get("unit"),
            "servings": entry.get("servings"),
            "nutrients": entry.get("nutrients"),
            "logged_at": entry.get("logged_at"),
        }
        return f"{SOURCE}:{source_date}:hash:{hashlib.sha256(_json(stable).encode()).hexdigest()}"

    def _ingest_occurrence(
        self, source_date: str, position: int, entry: dict[str, Any], snapshot_id: int,
        retrieved_at: str,
    ) -> int:
        stable_key = self._stable_key(source_date, position, entry)
        name_raw = str(entry.get("food_name") or "")
        brand_raw = str(entry.get("food_brand") or "")
        previous = self.connection.execute(
            """SELECT id,food_name_normalized,brand_normalized,source_food_id
               FROM food_occurrences WHERE stable_key=?""",
            (stable_key,),
        ).fetchone()
        existed = previous is not None
        self.connection.execute(
            """INSERT INTO food_occurrences
               (stable_key,source,source_date,meal,food_name_raw,food_name_normalized,
                brand_raw,brand_normalized,amount,unit,servings,source_food_id,
                source_entry_id,source_logged_at,ingestion_timestamp,raw_snapshot_id)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(stable_key) DO UPDATE SET
                 meal=excluded.meal,
                 food_name_raw=excluded.food_name_raw,
                 food_name_normalized=excluded.food_name_normalized,
                 brand_raw=excluded.brand_raw,
                 brand_normalized=excluded.brand_normalized,
                 amount=excluded.amount,
                 unit=excluded.unit,
                 servings=excluded.servings,
                 source_food_id=excluded.source_food_id,
                 source_entry_id=excluded.source_entry_id,
                 source_logged_at=excluded.source_logged_at,
                 ingestion_timestamp=excluded.ingestion_timestamp,
                 raw_snapshot_id=excluded.raw_snapshot_id""",
            (stable_key, SOURCE, source_date, entry.get("meal"), name_raw, normalize_text(name_raw),
             brand_raw, normalize_text(brand_raw), entry.get("amount"), entry.get("unit"),
             entry.get("servings"), entry.get("food_id"), entry.get("entry_id"),
             entry.get("logged_at"), retrieved_at, snapshot_id),
        )
        row = self.connection.execute(
            "SELECT id FROM food_occurrences WHERE stable_key=?", (stable_key,)
        ).fetchone()
        assert row is not None
        if previous is not None and (
            previous["food_name_normalized"] != normalize_text(name_raw)
            or previous["brand_normalized"] != normalize_text(brand_raw)
            or previous["source_food_id"] != entry.get("food_id")
        ):
            self.connection.execute(
                "DELETE FROM occurrence_reference_links WHERE occurrence_id=?",
                (int(row["id"]),),
            )
        # The normalized layer represents the latest source snapshot. Raw
        # history remains append-only, so replacing these projections loses no
        # evidence and prevents a nutrient removed upstream from lingering.
        self.connection.execute(
            "DELETE FROM nutrient_observations WHERE occurrence_id=? AND provenance='loseit'",
            (int(row["id"]),),
        )
        nutrients = dict(entry.get("nutrients") or {})
        if entry.get("calories") is not None:
            nutrients.setdefault("calories", entry["calories"])
        for nutrient, value in nutrients.items():
            if not isinstance(value, int | float):
                continue
            self.connection.execute(
                """INSERT INTO nutrient_observations
                   (occurrence_id,nutrient,value,unit,provenance) VALUES (?,?,?,?, 'loseit')
                   ON CONFLICT(occurrence_id,nutrient,provenance) DO UPDATE SET
                     value=excluded.value, unit=excluded.unit""",
                (int(row["id"]), str(nutrient), float(value), nutrient_unit(str(nutrient))),
            )
        return int(not existed)

    def _match_or_queue(
        self, source_date: str, position: int, entry: dict[str, Any], *, increment_queue: bool
    ) -> tuple[int, int]:
        stable_key = self._stable_key(source_date, position, entry)
        occurrence = self.connection.execute(
            "SELECT * FROM food_occurrences WHERE stable_key=?", (stable_key,)
        ).fetchone()
        assert occurrence is not None
        if self.connection.execute(
            "SELECT 1 FROM occurrence_reference_links WHERE occurrence_id=?", (occurrence["id"],)
        ).fetchone():
            return 1, 0
        name, brand = occurrence["food_name_normalized"], occurrence["brand_normalized"]
        target = self.connection.execute(
            """SELECT reference_id FROM source_reference_targets
               WHERE source=? AND source_food_id=? AND food_name_normalized=? AND brand_normalized=?""",
            (SOURCE, occurrence["source_food_id"], name, brand),
        ).fetchone()
        if target is not None:
            self.connection.execute(
                """INSERT INTO occurrence_reference_links
                   (occurrence_id,reference_id,match_method,confidence,linked_at)
                   VALUES (?,?,'source_food_id','high',?)""",
                (occurrence["id"], target["reference_id"], _now()),
            )
            return 1, 0
        reference = self.connection.execute(
            """SELECT id FROM food_references WHERE food_name_normalized=? AND brand_normalized=?
               AND id NOT IN (SELECT reference_id FROM source_reference_targets)""",
            (name, brand),
        ).fetchone()
        method = "exact_normalized_name_brand"
        if reference is None:
            reference = self.connection.execute(
                """SELECT reference_id AS id FROM food_aliases
                   WHERE alias_name_normalized=? AND alias_brand_normalized=?
                     AND manually_confirmed=1""",
                (name, brand),
            ).fetchone()
            method = "manually_confirmed_alias"
            if reference is not None and self.connection.execute(
                "SELECT 1 FROM source_reference_targets WHERE reference_id=?", (reference["id"],)
            ).fetchone():
                reference = None
        if reference is not None:
            self.connection.execute(
                """INSERT OR IGNORE INTO occurrence_reference_links
                   (occurrence_id,reference_id,match_method,confidence,linked_at)
                   VALUES (?,?,?,'high',?)""",
                (occurrence["id"], reference["id"], method, _now()),
            )
            return 1, 0

        suggestions = self._fuzzy_suggestions(name, brand)
        source_food_id = str(entry.get("food_id") or "")
        existing = self.connection.execute(
            """SELECT id FROM enrichment_queue WHERE food_name_normalized=?
               AND brand_normalized=? AND source_food_id=?""",
            (name, brand, source_food_id),
        ).fetchone()
        now = _now()
        if existing:
            if increment_queue:
                self.connection.execute(
                    """UPDATE enrichment_queue SET occurrence_count=occurrence_count+1,
                       last_seen_at=?, fuzzy_suggestions_json=? WHERE id=?""",
                    (now, _json(suggestions), existing["id"]),
                )
            return 0, 0
        self.connection.execute(
            """INSERT INTO enrichment_queue
               (food_name_normalized,brand_normalized,source_food_id,reason,
                fuzzy_suggestions_json,first_seen_at,last_seen_at)
               VALUES (?,?,?,?,?,?,?)""",
            (name, brand, source_food_id, "no high-confidence cached reference",
             _json(suggestions), now, now),
        )
        return 0, 1

    def _fuzzy_suggestions(self, name: str, brand: str) -> list[dict[str, Any]]:
        suggestions: list[dict[str, Any]] = []
        for row in self.connection.execute(
            "SELECT id,food_name,brand,food_name_normalized,brand_normalized FROM food_references"
        ):
            name_score = SequenceMatcher(None, name, row["food_name_normalized"]).ratio()
            brand_score = 1.0 if brand == row["brand_normalized"] else SequenceMatcher(
                None, brand, row["brand_normalized"]
            ).ratio()
            score = round(name_score * 0.8 + brand_score * 0.2, 3)
            if score >= 0.75:
                suggestions.append({"reference_id": row["id"], "food_name": row["food_name"],
                                    "brand": row["brand"], "score": score})
        return sorted(suggestions, key=lambda item: item["score"], reverse=True)[:3]

    def import_enrichment(self, document: dict[str, Any], *, manage_transaction: bool = True) -> dict[str, Any]:
        """Append a researched version; never edit source observations."""
        from .research_queue import validate_import_target

        target, context = validate_import_target(self.connection, document)
        name = str(document.get("food_name") or "").strip()
        brand = str(document.get("brand") or "").strip()
        if not name:
            raise ValueError("food_name is required")
        match_type = str(document.get("match_type") or "")
        if match_type not in MATCH_TYPES:
            raise ValueError(f"match_type must be one of: {', '.join(sorted(MATCH_TYPES))}")
        confidence = str(document.get("confidence") or "")
        if confidence not in {"low", "medium", "high"}:
            raise ValueError("confidence must be low, medium, or high")
        source_reference = str(document.get("source_reference") or "").strip()
        assumptions = str(document.get("assumptions") or "").strip()
        research_date = str(document.get("research_date") or "").strip()
        if not source_reference or not assumptions or not research_date:
            raise ValueError("source_reference, assumptions, and research_date are required")
        nutrients = document.get("nutrients")
        if not isinstance(nutrients, list) or not nutrients:
            raise ValueError("nutrients must be a non-empty list")
        with self.connection if manage_transaction else nullcontext():
            self.connection.execute(
                """INSERT OR IGNORE INTO food_references
                   (food_name,brand,food_name_normalized,brand_normalized,created_at)
                   VALUES (?,?,?,?,?)""",
                (name, brand, normalize_text(name), normalize_text(brand), _now()),
            )
            reference = self.connection.execute(
                """SELECT id FROM food_references WHERE food_name_normalized=?
                   AND brand_normalized=?""",
                (normalize_text(name), normalize_text(brand)),
            ).fetchone()
            assert reference is not None
            bound = self.connection.execute(
                "SELECT * FROM source_reference_targets WHERE reference_id=?", (reference["id"],)
            ).fetchall()
            if bound and (target is None or any(r["source_food_id"] != target for r in bound)):
                raise ValueError("Reference is bound to a different source food ID; explicit review required")
            if target is not None:
                foreign_links = self.connection.execute(
                    """SELECT 1 FROM occurrence_reference_links l JOIN food_occurrences o
                       ON o.id=l.occurrence_id WHERE l.reference_id=?
                       AND (o.source!=? OR COALESCE(o.source_food_id,'')!=?)""",
                    (reference["id"], SOURCE, target),
                ).fetchone()
                if foreign_links:
                    raise ValueError("Reference already links other source foods; explicit review required")
                if document.get("aliases"):
                    raise ValueError("ID-targeted imports cannot add name-only aliases")
                existing = self.connection.execute(
                    "SELECT reference_id FROM source_reference_targets WHERE source=? AND source_food_id=?",
                    (SOURCE, target),
                ).fetchall()
                if existing and any(r[0] != reference["id"] for r in existing):
                    raise ValueError("Source identity conflicts with a prior reference; explicit review required")
                self.connection.execute(
                    """INSERT OR IGNORE INTO source_reference_targets
                       VALUES (?,?,?,?,?,?)""",
                    (SOURCE, target, normalize_text(name), normalize_text(brand), reference["id"], _now()),
                )
            row = self.connection.execute(
                "SELECT COALESCE(MAX(version),0)+1 AS version FROM enrichment_versions WHERE reference_id=?",
                (reference["id"],),
            ).fetchone()
            version = int(row["version"])
            cursor = self.connection.execute(
                """INSERT INTO enrichment_versions
                   (reference_id,version,source_reference,reference_url,match_type,confidence,
                    assumptions,research_date,manually_reviewed,created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (reference["id"], version, source_reference, document.get("reference_url"),
                 match_type, confidence, assumptions, research_date,
                 int(bool(document.get("manually_reviewed"))), _now()),
            )
            enrichment_id = int(cursor.lastrowid)
            if target is not None:
                self.connection.execute(
                    "INSERT INTO enrichment_review_contexts VALUES (?,?,?)",
                    (enrichment_id, _json(context), _json(document["nutrition_basis"])),
                )
            for item in nutrients:
                provenance = str(item.get("provenance") or "")
                if provenance not in PROVENANCE_TYPES:
                    raise ValueError(
                        f"Every estimated nutrient needs provenance in: {', '.join(sorted(PROVENANCE_TYPES))}"
                    )
                self.connection.execute(
                    """INSERT INTO estimated_nutrients
                       (enrichment_version_id,nutrient,estimated_value,lower_bound,upper_bound,unit,provenance)
                       VALUES (?,?,?,?,?,?,?)""",
                    (enrichment_id, item["nutrient"], item.get("estimated_value"),
                     item.get("lower_bound"), item.get("upper_bound"),
                     item.get("unit") or nutrient_unit(str(item["nutrient"])), provenance),
                )
            for alias in document.get("aliases") or []:
                alias_name = normalize_text(str(alias.get("food_name") or ""))
                alias_brand = normalize_text(str(alias.get("brand") or ""))
                if not alias_name:
                    raise ValueError("every alias requires food_name")
                confirmed = int(bool(alias.get("manually_confirmed")))
                self.connection.execute(
                    """INSERT INTO food_aliases
                       (reference_id,alias_name_normalized,alias_brand_normalized,
                        manually_confirmed,created_at) VALUES (?,?,?,?,?)
                       ON CONFLICT(alias_name_normalized,alias_brand_normalized) DO UPDATE SET
                         reference_id=excluded.reference_id,
                         manually_confirmed=excluded.manually_confirmed""",
                    (reference["id"], alias_name, alias_brand, confirmed, _now()),
                )
                if confirmed:
                    self._link_existing_alias(int(reference["id"]), alias_name, alias_brand)
            if target is None:
                self._link_existing_reference(int(reference["id"]), normalize_text(name), normalize_text(brand))
            else:
                self.connection.execute(
                    """INSERT INTO occurrence_reference_links
                       (occurrence_id,reference_id,match_method,confidence,linked_at)
                       SELECT id,?,'source_food_id',?,? FROM food_occurrences
                       WHERE source=? AND source_food_id=? AND food_name_normalized=? AND brand_normalized=?
                       ON CONFLICT(occurrence_id) DO UPDATE SET reference_id=excluded.reference_id,
                         match_method=excluded.match_method,confidence=excluded.confidence,
                         linked_at=excluded.linked_at""",
                    (reference["id"], confidence, _now(), SOURCE, target,
                     normalize_text(name), normalize_text(brand)),
                )
        return {"reference_id": int(reference["id"]), "version": version,
                "nutrient_count": len(nutrients)}

    def _link_existing_reference(self, reference_id: int, name: str, brand: str) -> None:
        now = _now()
        self.connection.execute(
            """INSERT OR IGNORE INTO occurrence_reference_links
               (occurrence_id,reference_id,match_method,confidence,linked_at)
               SELECT id,?,'exact_normalized_name_brand','high',? FROM food_occurrences
               WHERE food_name_normalized=? AND brand_normalized=?""",
            (reference_id, now, name, brand),
        )
        self.connection.execute(
            """UPDATE enrichment_queue SET status='resolved', last_seen_at=?
               WHERE food_name_normalized=? AND brand_normalized=?""",
            (now, name, brand),
        )

    def _link_existing_alias(self, reference_id: int, name: str, brand: str) -> None:
        if self.connection.execute(
            "SELECT 1 FROM source_reference_targets WHERE reference_id=?", (reference_id,)
        ).fetchone():
            raise ValueError("A source-ID reference cannot be applied by a name-only alias")
        now = _now()
        self.connection.execute(
            """INSERT OR IGNORE INTO occurrence_reference_links
               (occurrence_id,reference_id,match_method,confidence,linked_at)
               SELECT id,?,'manually_confirmed_alias','high',? FROM food_occurrences
               WHERE food_name_normalized=? AND brand_normalized=?""",
            (reference_id, now, name, brand),
        )
        self.connection.execute(
            """UPDATE enrichment_queue SET status='resolved', last_seen_at=?
               WHERE food_name_normalized=? AND brand_normalized=?""",
            (now, name, brand),
        )

    def unresolved(self) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """SELECT * FROM enrichment_queue WHERE status='unresolved'
               ORDER BY occurrence_count DESC, last_seen_at DESC"""
        ).fetchall()
        return [dict(row) | {"fuzzy_suggestions": json.loads(row["fuzzy_suggestions_json"])}
                for row in rows]

    def ingest_weights(self, payload: dict[str, Any], *, retrieved_at: str) -> dict[str, int]:
        """Store an exact raw response and idempotent per-day weight rows."""
        serialized = _json(payload)
        digest = hashlib.sha256(serialized.encode()).hexdigest()
        start, end = str(payload["start"]), str(payload["end"])
        directory = self.raw_dir / "weights"
        directory.mkdir(parents=True, exist_ok=True)
        stamp = re.sub(r"[^0-9]", "", retrieved_at)[:14]
        raw_file = directory / f"{start}_{end}T{stamp}-{digest[:12]}.json"
        if not raw_file.exists():
            raw_file.write_text(serialized + "\n", encoding="utf-8")
        summary = {"weight_snapshots_added": 0, "weights_seen": 0, "weights_added": 0}
        with self.connection:
            before = self.connection.total_changes
            self.connection.execute(
                """INSERT OR IGNORE INTO raw_weight_snapshots
                   (source,start_date,end_date,retrieved_at,content_sha256,payload_json,file_path)
                   VALUES (?,?,?,?,?,?,?)""",
                (SOURCE, start, end, retrieved_at, digest, serialized, str(raw_file)),
            )
            summary["weight_snapshots_added"] = self.connection.total_changes - before
            for entry in payload.get("entries") or []:
                if not isinstance(entry.get("weight"), int | float):
                    continue
                summary["weights_seen"] += 1
                before = self.connection.total_changes
                self.connection.execute(
                    """INSERT OR IGNORE INTO weight_observations
                       (source,source_date,weight,unit,retrieved_at) VALUES (?,?,?,?,?)""",
                    (SOURCE, entry["date"], float(entry["weight"]), entry.get("unit"), retrieved_at),
                )
                summary["weights_added"] += self.connection.total_changes - before
                self.connection.execute(
                    "UPDATE weight_observations SET weight=?,unit=?,retrieved_at=? WHERE source=? AND source_date=?",
                    (float(entry["weight"]), entry.get("unit"), retrieved_at, SOURCE, entry["date"]),
                )
        return summary


def current_filter(connection: sqlite3.Connection) -> str:
    """Support read-only schema 1/2 reports without an implicit migration."""
    columns = {r[1] for r in connection.execute("PRAGMA table_info(food_occurrences)")}
    return " WHERE is_current=1" if "is_current" in columns else ""


def normalize_text(value: str) -> str:
    text = unicodedata.normalize("NFKC", value).casefold()
    text = re.sub(r"[^\w]+", " ", text, flags=re.UNICODE)
    return " ".join(text.split())


def nutrient_unit(nutrient: str) -> str:
    if nutrient == "calories":
        return "kcal"
    if nutrient.endswith("_mg"):
        return "mg"
    if nutrient.endswith("_g"):
        return "g"
    return "source_unit"


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
