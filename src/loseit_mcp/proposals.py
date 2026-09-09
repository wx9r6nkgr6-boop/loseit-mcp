"""Strict local proposal validation and append-only, atomic review decisions."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from urllib.parse import urlsplit

from .repository import (
    MATCH_TYPES,
    PROVENANCE_TYPES,
    STANDARD_NUTRIENTS,
    NutritionRepository,
    _now,
    nutrient_unit,
)
from .research_queue import _finite, _groups, _hash, _table, validate_import_target

MAX_BYTES = 1_000_000
TARGET_KEYS = {
    "source",
    "source_food_id",
    "food_name_normalized",
    "brand_normalized",
    "source_context_sha256",
}
REQUIRED = {
    "schema_version",
    "target",
    "food_name",
    "brand",
    "nutrients",
    "source_reference",
    "reference_url",
    "match_type",
    "confidence",
    "assumptions",
    "nutrition_basis",
    "research_date",
}
OPTIONAL = {"notes", "mismatch_explanation"}
SETTING_KEYS = {
    "protein_target",
    "calorie_min",
    "calorie_max",
    "fiber_target",
    "sodium_limit",
    "sugar_target",
}


class ProposalError(ValueError):
    """Safe, field-oriented validation error suitable for display."""


def canonical(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ProposalError("Duplicate JSON keys are not allowed")
        result[key] = value
    return result


def parse_proposal(data: bytes) -> dict:
    if not isinstance(data, bytes) or len(data) > MAX_BYTES:
        raise ProposalError("Proposal must be UTF-8 JSON of at most 1 MB")
    try:
        document = json.loads(
            data.decode("utf-8"),
            object_pairs_hook=_pairs,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ProposalError("Nonfinite JSON numbers are not allowed")
            ),
        )
    except (UnicodeError, json.JSONDecodeError, RecursionError):
        raise ProposalError("Malformed UTF-8 JSON proposal") from None
    validate_document(document)
    return document


def _text(value, field, *, empty=False):
    if not isinstance(value, str) or len(value) > 8000 or (not empty and not value.strip()):
        raise ProposalError(f"{field} must be a valid text field")
    if any(ord(c) < 32 and c not in "\n\t" for c in value):
        raise ProposalError(f"{field} contains control characters")


def validate_document(doc):
    if not isinstance(doc, dict) or not REQUIRED <= doc.keys() or doc.keys() - REQUIRED - OPTIONAL:
        raise ProposalError("Proposal has missing or unsupported fields")
    if type(doc["schema_version"]) is not int or doc["schema_version"] != 1:
        raise ProposalError("Supported proposal schema_version is 1")
    target = doc["target"]
    if not isinstance(target, dict) or set(target) != TARGET_KEYS or target["source"] != "loseit":
        raise ProposalError("A complete stable Lose It import target is required")
    for key in TARGET_KEYS:
        _text(target[key], "target." + key, empty=key == "brand_normalized")
    if len(target["source_context_sha256"]) != 64 or any(
        c not in "0123456789abcdef" for c in target["source_context_sha256"]
    ):
        raise ProposalError("Source context hash must be a SHA-256 hex digest")
    for key in (
        "food_name",
        "brand",
        "source_reference",
        "reference_url",
        "assumptions",
        "research_date",
        *OPTIONAL,
    ):
        if key in doc:
            _text(doc[key], key, empty=key in {"brand", *OPTIONAL})
    url = urlsplit(doc["reference_url"])
    if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password:
        raise ProposalError("Reference URL must be an HTTP(S) URL without embedded credentials")
    try:
        researched = date.fromisoformat(doc["research_date"])
    except ValueError:
        raise ProposalError("research_date must use YYYY-MM-DD") from None
    if researched > date.today():  # noqa: DTZ011 - user-facing local research date
        raise ProposalError("research_date cannot be in the future")
    if (
        not isinstance(doc["match_type"], str)
        or not isinstance(doc["confidence"], str)
        or doc["match_type"] not in MATCH_TYPES
        or doc["confidence"] not in {"low", "medium", "high"}
    ):
        raise ProposalError("Unsupported match type or confidence")
    basis = doc["nutrition_basis"]
    if (
        not isinstance(basis, dict)
        or not {"amount", "unit", "description"} <= basis.keys()
        or basis.keys() - {"amount", "unit", "description", "occurrence_scaling"}
    ):
        raise ProposalError("A documented nutrition_basis is required")
    if not _finite(basis["amount"]) or not 0 < basis["amount"] <= 1_000_000:
        raise ProposalError("Nutrition basis amount must be finite and positive")
    _text(basis["unit"], "nutrition_basis.unit")
    _text(basis["description"], "nutrition_basis.description")
    if "occurrence_scaling" in basis:
        scaling = basis["occurrence_scaling"]
        if (
            not isinstance(scaling, dict)
            or set(scaling) != {"method", "unit"}
            or scaling["method"] != "logged_amount"
            or scaling["unit"] != basis["unit"]
        ):
            raise ProposalError(
                "Scaling must explicitly map logged_amount using the exact basis unit"
            )
    nutrients = doc["nutrients"]
    if not isinstance(nutrients, list) or not 1 <= len(nutrients) <= 9:
        raise ProposalError("Provide between 1 and 9 standard nutrient records")
    seen = set()
    for item in nutrients:
        if (
            not isinstance(item, dict)
            or not {"nutrient", "unit", "provenance"} <= item.keys()
            or item.keys()
            - {"nutrient", "unit", "provenance", "estimated_value", "lower_bound", "upper_bound"}
        ):
            raise ProposalError("Malformed proposed nutrient record")
        n = item["nutrient"]
        if not isinstance(n, str) or n not in STANDARD_NUTRIENTS or n in seen:
            raise ProposalError("Unsupported or duplicate nutrient")
        seen.add(n)
        if (
            item["unit"] != nutrient_unit(n)
            or not isinstance(item["provenance"], str)
            or item["provenance"] not in PROVENANCE_TYPES
        ):
            raise ProposalError("Nutrient unit or provenance is invalid")
        point, lo, hi = (
            item.get("estimated_value"),
            item.get("lower_bound"),
            item.get("upper_bound"),
        )
        for value in (point, lo, hi):
            if value is not None and (not _finite(value) or not 0 <= value <= 1_000_000):
                raise ProposalError(
                    "Nutrient values must be finite, nonnegative and at most 1,000,000 per basis"
                )
        if point is None and (lo is None or hi is None):
            raise ProposalError("Each nutrient needs a point or complete uncertainty bounds")
        if (lo is None) != (hi is None) or (
            lo is not None and (lo > hi or (point is not None and not lo <= point <= hi))
        ):
            raise ProposalError(
                "Uncertainty bounds must be complete, ordered and contain the point"
            )


@contextmanager
def reader(data_dir):
    c = sqlite3.connect(
        (Path(data_dir).expanduser() / "nutrition.sqlite3").resolve().as_uri() + "?mode=ro",
        uri=True,
    )
    c.row_factory = sqlite3.Row
    try:
        c.execute("PRAGMA query_only=ON")
        c.execute("BEGIN")
        yield c
    finally:
        c.close()


def source_context(c, food_id):
    groups = [g for g in _groups(c) if g["source_food_id"] == food_id]
    if len(groups) != 1:
        raise ProposalError("Source food ID does not identify one current food")
    g = groups[0]
    return {
        "source_food_id": food_id,
        "portion_variants": g["variants"],
        "source_context_sha256": _hash(g["context"]),
        "source_basis": "Original label basis unavailable; these are stored occurrence values",
        "occurrence_count": len(g["rows"]),
    }


def _context_problem(c, doc):
    try:
        source_context(c, doc["target"]["source_food_id"])
        validate_import_target(c, doc | {"manually_reviewed": True})
    except (ValueError, TypeError):
        return "Source context is stale, conflicting, or mismatched; verify target and context before approval"
    return None


def _list(c):
    if not _table(c, "research_proposals"):
        return []
    result = []
    for row in c.execute("SELECT * FROM research_proposals ORDER BY id DESC"):
        doc = json.loads(row["document_json"])
        actions = [
            dict(a)
            for a in c.execute(
                "SELECT * FROM proposal_reviews WHERE proposal_id=? ORDER BY id", (row["id"],)
            )
        ]
        if actions and actions[-1]["document_json"]:
            doc = json.loads(actions[-1]["document_json"])
        approved = {
            n
            for a in actions
            if a["action"] in {"approved", "partially_approved"}
            for n in json.loads(a["selected_json"])
        }
        latest = actions[-1]["action"] if actions else "ready"
        if latest in {"imported", "needs_review"}:
            latest = "ready"
        problem = _context_problem(c, doc) if latest not in {"approved", "rejected"} else None
        if problem and latest != "deferred":
            latest = "needs_review"
        result.append(
            {
                "id": row["id"],
                "created_at": row["created_at"],
                "document": doc,
                "status": latest,
                "approved_nutrients": sorted(approved),
                "context_problem": problem,
                "history": actions,
                "conflicting_proposals": [],
            }
        )
    pending = [p for p in result if p["status"] not in {"approved", "rejected"}]
    for p in pending:
        for other in pending:
            if (
                p["id"] == other["id"]
                or p["document"]["target"]["source_food_id"]
                != other["document"]["target"]["source_food_id"]
            ):
                continue
            left = {
                n["nutrient"]: n
                for n in p["document"]["nutrients"]
                if n["nutrient"] not in p["approved_nutrients"]
            }
            right = {
                n["nutrient"]: n
                for n in other["document"]["nutrients"]
                if n["nutrient"] not in other["approved_nutrients"]
            }
            if any(
                left[n] != right[n]
                or p["document"]["nutrition_basis"] != other["document"]["nutrition_basis"]
                for n in left.keys() & right.keys()
            ):
                p["conflicting_proposals"].append(other["id"])
                if p["status"] != "deferred":
                    p["status"] = "needs_review"
    return result


def list_proposals(data_dir):
    with reader(data_dir) as c:
        return _list(c)


def import_proposal(data_dir, data):
    doc = parse_proposal(data)
    # Reject missing IDs before opening a writable repository/migration.
    with reader(data_dir) as c:
        source_context(c, doc["target"]["source_food_id"])
        problem = _context_problem(c, doc)
    digest = hashlib.sha256(canonical(doc).encode()).hexdigest()
    with NutritionRepository(Path(data_dir)) as repo, repo.connection as c:
        c.execute("BEGIN IMMEDIATE")
        prior = c.execute(
            "SELECT id FROM research_proposals WHERE content_sha256=?", (digest,)
        ).fetchone()
        if prior:
            return {"id": prior[0], "duplicate": True}
        pid = c.execute(
            "INSERT INTO research_proposals(content_sha256,source_food_id,document_json,created_at) VALUES (?,?,?,?)",
            (digest, doc["target"]["source_food_id"], canonical(doc), _now()),
        ).lastrowid
        c.execute(
            "INSERT INTO proposal_reviews(proposal_id,action,note,created_at) VALUES (?,?,?,?)",
            (
                pid,
                "needs_review" if problem else "imported",
                problem or "Imported locally; not approved",
                _now(),
            ),
        )
        return {"id": pid, "duplicate": False}


def review_proposal(
    data_dir,
    proposal_id,
    action,
    *,
    selected=None,
    edited_document=None,
    note="",
    acknowledge_conflicts=False,
):
    if action not in {"approve", "rejected", "deferred"}:
        raise ProposalError("Unsupported review action")
    _text(note, "Review note", empty=True)
    with NutritionRepository(Path(data_dir)) as repo, repo.connection as c:
        c.execute("BEGIN IMMEDIATE")
        proposal = next((p for p in _list(c) if p["id"] == proposal_id), None)
        if not proposal or proposal["status"] in {"approved", "rejected"}:
            raise ProposalError("Proposal is missing or already finalized")
        document = edited_document if edited_document is not None else proposal["document"]
        validate_document(document)
        if document["target"]["source_food_id"] != proposal["document"]["target"]["source_food_id"]:
            raise ProposalError("An edit cannot retarget a proposal to another source food")
        edited = document != proposal["document"]
        version_id = None
        chosen = []
        if action == "approve":
            if proposal["conflicting_proposals"] and not acknowledge_conflicts:
                raise ProposalError(
                    "Review conflicting proposals and explicitly acknowledge the conflict"
                )
            if _context_problem(c, document):
                raise ProposalError(
                    "Source context/identity is stale or conflicting; approval blocked"
                )
            if document["match_type"] == "insufficient_information":
                raise ProposalError(
                    "Insufficient-information proposals must be deferred or rejected"
                )
            nutrients = {n["nutrient"]: n for n in document["nutrients"]}
            if (
                not isinstance(selected, list)
                or not selected
                or len(selected) != len(set(selected))
                or not set(selected) <= nutrients.keys()
            ):
                raise ProposalError("Select valid distinct nutrients to approve")
            if set(selected) & set(proposal["approved_nutrients"]):
                raise ProposalError(
                    "These nutrients were already approved; import a new proposal to revise them"
                )
            chosen = selected
            merged = {}
            old = c.execute(
                "SELECT v.*,r.nutrition_basis_json FROM source_reference_targets t JOIN latest_enrichment_versions v ON v.reference_id=t.reference_id JOIN enrichment_review_contexts r ON r.enrichment_version_id=v.id WHERE t.source='loseit' AND t.source_food_id=?",
                (document["target"]["source_food_id"],),
            ).fetchone()
            if old:
                old_basis = json.loads(old["nutrition_basis_json"])
                retained = [
                    dict(r)
                    for r in c.execute(
                        "SELECT nutrient,estimated_value,lower_bound,upper_bound,unit,provenance FROM estimated_nutrients WHERE enrichment_version_id=?",
                        (old["id"],),
                    )
                    if r["nutrient"] not in chosen
                ]
                if retained and any(
                    old_basis.get(k) != document["nutrition_basis"].get(k)
                    for k in ("amount", "unit", "occurrence_scaling")
                ):
                    raise ProposalError(
                        "Retained nutrients have a different portion basis; review a complete replacement proposal"
                    )
                if retained and (
                    not old["manually_reviewed"]
                    or any(
                        old[k] != document[k]
                        for k in (
                            "source_reference",
                            "reference_url",
                            "confidence",
                            "match_type",
                            "assumptions",
                            "research_date",
                        )
                    )
                ):
                    raise ProposalError(
                        "Retained nutrients have different evidence/confidence; use a complete replacement to preserve provenance"
                    )
                merged.update({n["nutrient"]: n for n in retained})
            merged.update({n: nutrients[n] for n in chosen})
            approved = {k: v for k, v in document.items() if k not in {"schema_version", *OPTIONAL}}
            approved.update(nutrients=list(merged.values()), manually_reviewed=True)
            result = repo.import_enrichment(approved, manage_transaction=False)
            version_id = c.execute(
                "SELECT id FROM enrichment_versions WHERE reference_id=? AND version=?",
                (result["reference_id"], result["version"]),
            ).fetchone()[0]
            remaining = set(nutrients) - set(proposal["approved_nutrients"]) - set(chosen)
            action = "partially_approved" if remaining else "approved"
        c.execute(
            "INSERT INTO proposal_reviews(proposal_id,action,document_json,selected_json,user_edited,enrichment_version_id,note,created_at) VALUES (?,?,?,?,?,?,?,?)",
            (
                proposal_id,
                action,
                canonical(document),
                canonical(chosen),
                int(edited),
                version_id,
                note,
                _now(),
            ),
        )
        return {"status": action, "enrichment_version_id": version_id}


def read_settings(data_dir):
    with reader(data_dir) as c:
        row = (
            c.execute(
                "SELECT settings_json FROM dashboard_settings_versions ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if _table(c, "dashboard_settings_versions")
            else None
        )
        return dict.fromkeys(SETTING_KEYS) | (json.loads(row[0]) if row else {})


def save_settings(data_dir, settings):
    if not isinstance(settings, dict) or set(settings) != SETTING_KEYS:
        raise ProposalError("Settings must include exactly the supported target fields")
    if any(
        v is not None and (not _finite(v) or not 0 <= v <= 1_000_000) for v in settings.values()
    ):
        raise ProposalError("Targets must be finite nonnegative numbers or null")
    low, high = settings["calorie_min"], settings["calorie_max"]
    if (low is None) != (high is None) or (low is not None and low > high):
        raise ProposalError("Calorie range requires both ordered limits")
    with NutritionRepository(Path(data_dir)) as repo, repo.connection as c:
        c.execute(
            "INSERT INTO dashboard_settings_versions(settings_json,created_at) VALUES (?,?)",
            (canonical(settings), _now()),
        )


def flag_source(data_dir, food_id, status, note):
    if status not in {"unreliable", "needs_review", "cleared"}:
        raise ProposalError("Unknown source review status")
    _text(note, "Flag explanation")
    with NutritionRepository(Path(data_dir)) as repo, repo.connection as c:
        source_context(c, food_id)
        c.execute(
            "INSERT INTO source_review_flags(source_food_id,status,note,created_at) VALUES (?,?,?,?)",
            (food_id, status, note, _now()),
        )


def review_context(data_dir, food_id):
    with reader(data_dir) as c:
        result = source_context(c, food_id)
        result["manual_flags"] = (
            [
                dict(r)
                for r in c.execute(
                    "SELECT status,note,created_at FROM source_review_flags WHERE source_food_id=? ORDER BY id",
                    (food_id,),
                )
            ]
            if _table(c, "source_review_flags")
            else []
        )
        result["enrichment_versions"] = []
        if _table(c, "source_reference_targets"):
            for row in c.execute(
                "SELECT v.* FROM enrichment_versions v JOIN source_reference_targets t ON t.reference_id=v.reference_id WHERE t.source_food_id=? ORDER BY v.id",
                (food_id,),
            ):
                version = dict(row)
                version["nutrients"] = [
                    dict(n)
                    for n in c.execute(
                        "SELECT nutrient,estimated_value,lower_bound,upper_bound,unit,provenance FROM estimated_nutrients WHERE enrichment_version_id=?",
                        (row["id"],),
                    )
                ]
                result["enrichment_versions"].append(version)
        return result


def proposal_preview(data_dir, document):
    validate_document(document)
    context = review_context(data_dir, document["target"]["source_food_id"])
    latest = context["enrichment_versions"][-1] if context["enrichment_versions"] else None
    old = {n["nutrient"]: n for n in latest["nutrients"]} if latest else {}
    changes = []
    for nutrient in document["nutrients"]:
        n = nutrient["nutrient"]
        values = sorted(
            {
                v["source_nutrients"][n]
                for v in context["portion_variants"]
                if n in v["source_nutrients"]
            }
        )
        source_gap = any(n not in v["source_nutrients"] for v in context["portion_variants"])
        changes.append(
            {
                "nutrient": n,
                "stored_source_values": values,
                "source_gap": source_gap,
                "previous_estimate": old.get(n, {}).get("estimated_value"),
                "proposed_estimate": nutrient.get("estimated_value"),
                "lower_bound": nutrient.get("lower_bound"),
                "upper_bound": nutrient.get("upper_bound"),
                "unit": nutrient["unit"],
                "effect": "Reference revision; source unchanged"
                if n in old
                else "Fill source gaps"
                if source_gap
                else "Reference only; source takes precedence",
            }
        )
    return {"source": context, "changes": changes}
