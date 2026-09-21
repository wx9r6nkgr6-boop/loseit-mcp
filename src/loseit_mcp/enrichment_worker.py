"""Evidence worker boundary and conservative missing-only acceptance policy.

The bundled worker uses four explicitly supplied, independently checked references.
It is NOT an open-web search implementation. Unmapped products fail closed.
No authentication, Lose It client, network client or UI imports belong here.
"""

from dataclasses import asdict, dataclass
from datetime import date
from typing import Protocol

from .proposals import ProposalError, validate_document
from .repository import STANDARD_NUTRIENTS, normalize_text, nutrient_unit
from .research_queue import _finite

POLICY_VERSION = "exact-label-v1"


@dataclass(frozen=True)
class Evidence:
    source_food_id: str
    food_name: str
    brand: str
    url: str
    title: str
    source_kind: str
    research_date: str
    nutrients: dict
    basis_amount: float
    basis_unit: str
    basis_description: str
    portion_reconciled: bool
    identity_verified: bool = True
    confidence: str = "high"
    concerns: tuple[str, ...] = ()
    match_type: str = "exact_brand_product"


class ResearchWorker(Protocol):
    """Implementations return evidence, never execute approval or receive auth/diary logs."""

    def research(self, candidate: dict) -> Evidence | None: ...


def make_proposal(candidate, evidence):
    if not isinstance(evidence, Evidence) or evidence.source_food_id != candidate["source_food_id"]:
        raise ProposalError("Research response has no matching stable identity")
    if not isinstance(evidence.nutrients, dict) or any(
        n not in STANDARD_NUTRIENTS or not _finite(v) or not 0 <= v <= 1_000_000
        for n, v in evidence.nutrients.items()
    ):
        raise ProposalError("Research contains invalid nutrient values")
    missing = set(candidate["missing_standard_nutrients"])
    doc = {
        "schema_version": 1,
        "target": candidate["import_target"],
        "food_name": candidate["food_name"],
        "brand": candidate["brand"],
        "source_reference": evidence.title,
        "reference_url": evidence.url,
        "match_type": evidence.match_type,
        "confidence": evidence.confidence,
        "research_date": evidence.research_date,
        "assumptions": "Evidence is per stated label basis; source values always win. "
        + (
            "Explicit portion mapping verified."
            if evidence.portion_reconciled
            else "Portion mapping not verified; human confirmation required."
        ),
        "nutrition_basis": {
            "amount": evidence.basis_amount,
            "unit": evidence.basis_unit,
            "description": evidence.basis_description,
        },
        "nutrients": [
            {
                "nutrient": n,
                "estimated_value": v,
                "unit": nutrient_unit(n),
                "provenance": "researched_exact_product",
            }
            for n, v in evidence.nutrients.items()
            if n in missing
        ],
        "mismatch_explanation": " ".join(evidence.concerns),
    }
    if evidence.portion_reconciled:
        doc["nutrition_basis"]["occurrence_scaling"] = {
            "method": "logged_amount",
            "unit": evidence.basis_unit,
        }
    validate_document(doc)
    return doc


def evaluate(evidence, document, context, *, today=None):
    """Every variant must match; no calorie-derived portion inference, no source overwrite.

    Fingerprint tolerance: max(0.5 kcal/g or 2 mg, 2% of expected value).
    Require calories plus >=3 existing non-calorie nutrient checks per variant.
    A zero source value is evidence; a missing value is never treated as zero.
    """
    validate_document(document)
    today = today or date.today()  # noqa: DTZ011
    reasons = list(evidence.concerns)
    if (
        document["reference_url"] != evidence.url
        or document["source_reference"] != evidence.title
        or document["research_date"] != evidence.research_date
        or document["confidence"] != evidence.confidence
        or document["match_type"] != evidence.match_type
    ):
        reasons.append("Proposal provenance does not match the verified evidence.")
    if evidence.source_kind not in {"manufacturer_label", "retailer_label"}:
        reasons.append("Secondary database evidence requires a product-label review.")
    if not evidence.identity_verified or evidence.confidence != "high":
        reasons.append("Exact product identity and high confidence are required.")
    if evidence.source_food_id != context["source_food_id"] or any(
        normalize_text(value) != document["target"][key]
        for value, key in (
            (evidence.food_name, "food_name_normalized"),
            (evidence.brand, "brand_normalized"),
        )
    ):
        reasons.append("Research product identity does not match the source record.")
    age = (today - date.fromisoformat(evidence.research_date)).days
    if not 0 <= age <= 90:
        reasons.append("Evidence must be revalidated within 90 days.")
    if not evidence.portion_reconciled:
        reasons.append("Confirm the logged portion against the physical label before applying.")
    basis = document["nutrition_basis"]
    if (
        basis.get("occurrence_scaling") != {"method": "logged_amount", "unit": evidence.basis_unit}
        or basis["amount"] != evidence.basis_amount
        or basis["unit"] != evidence.basis_unit
    ):
        reasons.append("Explicit, matching portion scaling is required.")
    # Label sanity bounds, independent of the generic import cap.
    if any(
        not _finite(v)
        or v < 0
        or v > (10000 if n.endswith("_mg") else 2000 if n == "calories" else 250)
        for n, v in evidence.nutrients.items()
    ):
        reasons.append("Label nutrition is outside automatic acceptance bounds.")
    for child, parent in (
        ("saturated_fat_g", "total_fat_g"),
        ("fiber_g", "carb_g"),
        ("sugar_g", "carb_g"),
    ):
        if (
            child in evidence.nutrients
            and parent in evidence.nutrients
            and evidence.nutrients[child] > evidence.nutrients[parent]
        ):
            reasons.append("Label subnutrient exceeds its parent nutrient.")
    for variant in context["portion_variants"]:
        portion, source = variant["logged_portion"], variant["source_nutrients"]
        if (
            portion["unit"] != evidence.basis_unit
            or not _finite(portion["amount"])
            or not 0 < portion["amount"] <= 20 * evidence.basis_amount
        ):
            reasons.append("Serving unit/amount mismatch or suspicious scaling.")
            continue
        scale = portion["amount"] / evidence.basis_amount
        shared = set(source) & set(evidence.nutrients)
        if "calories" not in shared or len(shared - {"calories"}) < 3:
            reasons.append(
                "Insufficient existing nutrient fingerprint; calorie-only matching is unsafe."
            )
        for n in shared:
            expected = evidence.nutrients[n] * scale
            tolerance = max(2 if n.endswith("_mg") else 0.5, abs(expected) * 0.02)
            if abs(source[n] - expected) > tolerance:
                reasons.append(f"Source {n} contradicts label at the logged portion.")
        # A proposal may target a nutrient missing in only some variants. Source wins,
        # but an automatic action must never encode a conflicting replacement.
    if not context["portion_variants"]:
        reasons.append("No current source context.")
    for n in document["nutrients"]:
        if (
            n.get("estimated_value") != evidence.nutrients.get(n["nutrient"])
            or n.get("provenance") != "researched_exact_product"
        ):
            reasons.append("Proposed values are not direct evidence values.")
    return {
        "policy": POLICY_VERSION,
        "auto_approve": not reasons,
        "reasons": sorted(set(reasons)),
        "evidence": asdict(evidence),
    }


class CuratedEvidenceWorker:
    """Finite, verified seed migration; unmapped foods explicitly remain unresolved."""

    capability = "Four verified reference seeds; general web research is not configured."

    def research(self, candidate):
        return SEEDS.get(candidate["source_food_id"])


SEEDS = {
    "4f53d67a020b659a73482c6a699e2e1b": Evidence(
        "4f53d67a020b659a73482c6a699e2e1b",
        "frosted sugar cookie",
        "walmart",
        "https://www.eatthismuch.com/calories/frosted-sugar-cookies-4049651",
        "Eat This Much — Walmart Frosted Sugar Cookies",
        "secondary_database",
        "2026-09-20",
        {
            "calories": 160,
            "protein_g": 1,
            "carb_g": 26,
            "total_fat_g": 6,
            "saturated_fat_g": 3,
            "fiber_g": 0,
            "sugar_g": 16,
            "sodium_mg": 105,
            "cholesterol_mg": 5,
        },
        1,
        "serving",
        "1 cookie (38 g); supplied mapping is 1 logged serving, not independently proven by calories alone",
        False,
    ),
    "f0e5f283347d4cad3140e9b34bf8e2a8": Evidence(
        "f0e5f283347d4cad3140e9b34bf8e2a8",
        "mccafé iced mocha medium",
        "mcdonald s",
        "https://www.calorieking.com/us/en/foods/f/calories-in-beverages-iced-mocha-with-whole-milk-whipped-light-cream-chocolate-drizzle-medium/iEpvyYJzR0O3yiO6mLt4-g",
        "CalorieKing — McDonald's medium iced mocha, whole milk, whipped cream and drizzle",
        "secondary_database",
        "2026-09-20",
        {
            "calories": 320,
            "protein_g": 8,
            "carb_g": 48,
            "total_fat_g": 11,
            "sugar_g": 43,
            "saturated_fat_g": 7,
            "fiber_g": 2,
            "sodium_mg": 125,
            "cholesterol_mg": 35,
        },
        1,
        "serving",
        "1 medium beverage with whole milk, whipped cream and chocolate drizzle",
        True,
    ),
    "62b68f9ad47cee9da84c9074dd831d45": Evidence(
        "62b68f9ad47cee9da84c9074dd831d45",
        "cheese sharp cheddar",
        "great value",
        "https://www.instacart.com/products/20578093-great-value-deli-style-sliced-sharp-cheddar-cheese-12-ct",
        "Instacart — Great Value Sharp Cheddar Deli Style Sliced Cheese",
        "retailer_label",
        "2026-09-20",
        {
            "calories": 80,
            "protein_g": 5,
            "carb_g": 0,
            "total_fat_g": 6,
            "saturated_fat_g": 3.5,
            "sodium_mg": 120,
            "cholesterol_mg": 20,
            "sugar_g": 0,
            "fiber_g": 0,
        },
        1,
        "slice",
        "1 slice / 19 g; 12-count product, 12 servings; supplied per-slice identity cross-checked against label",
        True,
    ),
    "49c67296822d7b85b94fd3793a3e8465": Evidence(
        "49c67296822d7b85b94fd3793a3e8465",
        "oreo frozen dessert mini cones",
        "oreo",
        "https://www.kroger.com/p/oreo-frozen-dairy-mini-dessert-cones-6-count/0007255400014",
        "Kroger — OREO Frozen Dairy Mini Dessert Cones",
        "retailer_label",
        "2026-09-20",
        {
            "calories": 220,
            "protein_g": 2,
            "carb_g": 36,
            "total_fat_g": 8,
            "saturated_fat_g": 6,
            "fiber_g": 1,
            "sugar_g": 23,
            "sodium_mg": 105,
            "cholesterol_mg": 0,
        },
        2,
        "piece",
        "2 cones (84 g)",
        False,
        concerns=(
            "Lose It records 1 piece but its 220-calorie fingerprint matches 2 cones. Confirm what one logged piece means; do not infer scaling.",
        ),
    ),
}
