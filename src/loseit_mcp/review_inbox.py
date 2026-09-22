"""Simple factual-question projection over the append-only technical review history."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .proposals import canonical, list_proposals, reader
from .repository import NutritionRepository, _now
from .research_queue import read_research_queue


def _context_sha(value: dict) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def _question(food: dict, *, reason: str, proposal_id: int | None = None) -> dict:
    name = food["food_name_raw"] or food["food_name"]
    normalized = food["food_name"].casefold()
    variants = food.get("portion_variants") or []
    amounts = sorted(
        {
            row["logged_portion"]["amount"]
            for row in variants
            if isinstance(row.get("logged_portion", {}).get("amount"), int | float)
        }
    )
    if "oatnut" in normalized:
        prompt, choices, unit = "How many slices did you actually eat?", [2, 4], "slices"
    elif "oreo" in normalized and "cone" in normalized:
        prompt, choices, unit = "How many cones did you actually eat?", [1, 2], "cones"
    elif amounts:
        unit = str(variants[0]["logged_portion"].get("unit") or "units")
        prompt = f"How many {unit} did you actually eat?"
        choices = amounts[:3]
    else:
        unit = "servings"
        prompt = "How many servings did you actually eat?"
        choices = []
    context = {
        "source_food_id": food["source_food_id"],
        "food_name": food["food_name"],
        "brand": food["brand"],
        "portion_variants": variants,
        "reason": reason,
        "proposal_id": proposal_id,
    }
    return {
        "source_food_id": food["source_food_id"],
        "food_name": name,
        "brand": food["brand_raw"] or food["brand"],
        "question_kind": "actual_quantity",
        "question": prompt,
        "choices": [{"value": value, "label": f"{value:g} {unit}"} for value in choices],
        "unit": unit,
        "why": reason,
        "proposal_id": proposal_id,
        "context_sha256": _context_sha(context),
        "advanced": context,
    }


def needs_help(data_dir: Path) -> list[dict]:
    queue = read_research_queue(data_dir)
    foods = {row["source_food_id"]: row for row in queue["foods"] if row["source_food_id"]}
    proposals = list_proposals(data_dir)
    reasons: dict[str, tuple[str, int | None]] = {}
    for proposal in proposals:
        if proposal["status"] in {"needs_review", "ready", "partially_approved"}:
            food_id = proposal["document"]["target"]["source_food_id"]
            note = proposal["history"][-1]["note"] or "The evidence needs one factual check."
            reasons[food_id] = (note, proposal["id"])
    with reader(data_dir) as c:
        if c.execute("SELECT 1 FROM sqlite_master WHERE name='source_review_flags'").fetchone():
            for row in c.execute(
                """SELECT f.source_food_id,f.status,f.note FROM source_review_flags f
                   JOIN (SELECT source_food_id,MAX(id) id FROM source_review_flags GROUP BY source_food_id) x
                     ON x.id=f.id WHERE f.status!='cleared'"""
            ):
                reasons.setdefault(row["source_food_id"], (row["note"], None))
        answered = {
            (row["source_food_id"], row["context_sha256"])
            for row in c.execute(
                "SELECT source_food_id,context_sha256 FROM human_review_answers"
            )
        } if c.execute(
            "SELECT 1 FROM sqlite_master WHERE name='human_review_answers'"
        ).fetchone() else set()
    result = []
    for food_id, (reason, proposal_id) in reasons.items():
        food = foods.get(food_id)
        if not food:
            continue
        question = _question(food, reason=reason, proposal_id=proposal_id)
        if (food_id, question["context_sha256"]) not in answered:
            result.append(question)
    return sorted(result, key=lambda row: (row["food_name"].casefold(), row["source_food_id"]))


def answer_question(
    data_dir: Path,
    question: dict,
    answer,
    *,
    disposition: str = "answered",
) -> dict:
    if disposition not in {"answered", "unknown", "deferred"}:
        raise ValueError("Unsupported answer disposition")
    if disposition == "answered":
        if not isinstance(answer, int | float) or isinstance(answer, bool) or answer <= 0:
            raise ValueError("A positive quantity is required")
        payload = {"quantity": float(answer), "unit": question["unit"]}
    else:
        payload = {"answer": "unknown" if disposition == "unknown" else "deferred"}
    with NutritionRepository(Path(data_dir)) as repo, repo.connection as c:
        c.execute(
            """INSERT INTO human_review_answers
               (source_food_id,question_kind,context_sha256,answer_json,disposition,created_at)
               VALUES (?,?,?,?,?,?)""",
            (
                question["source_food_id"],
                question["question_kind"],
                question["context_sha256"],
                json.dumps(payload, sort_keys=True),
                disposition,
                _now(),
            ),
        )
    return {"status": disposition, "source_food_id": question["source_food_id"]}


def latest_answer(data_dir: Path, source_food_id: str) -> dict | None:
    with reader(data_dir) as c:
        exists = c.execute(
            "SELECT 1 FROM sqlite_master WHERE name='human_review_answers'"
        ).fetchone()
        row = (
            c.execute(
                """SELECT question_kind,answer_json,disposition,context_sha256,created_at
                   FROM human_review_answers WHERE source_food_id=? ORDER BY id DESC LIMIT 1""",
                (source_food_id,),
            ).fetchone()
            if exists
            else None
        )
    return (
        {
            "question_kind": row[0],
            "answer": json.loads(row[1]),
            "disposition": row[2],
            "context_sha256": row[3],
            "created_at": row[4],
        }
        if row
        else None
    )


def recent_answers(data_dir: Path, limit: int = 10) -> list[dict]:
    if not 1 <= limit <= 50:
        raise ValueError("Answer history limit must be between 1 and 50")
    with reader(data_dir) as c:
        if not c.execute(
            "SELECT 1 FROM sqlite_master WHERE name='human_review_answers'"
        ).fetchone():
            return []
        rows = c.execute(
            """SELECT a.source_food_id,a.answer_json,a.disposition,a.created_at,
                      COALESCE((SELECT o.food_name_normalized FROM food_occurrences o
                                WHERE o.source_food_id=a.source_food_id AND o.is_current=1
                                ORDER BY o.id DESC LIMIT 1),'') food_name,
                      COALESCE((SELECT o.brand_normalized FROM food_occurrences o
                                WHERE o.source_food_id=a.source_food_id AND o.is_current=1
                                ORDER BY o.id DESC LIMIT 1),'') brand
               FROM human_review_answers a ORDER BY a.id DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    return [
        {
            "food_name": row[4],
            "brand": row[5],
            "answer": json.loads(row[1]),
            "status": row[2],
            "saved_at": row[3],
        }
        for row in rows
    ]
