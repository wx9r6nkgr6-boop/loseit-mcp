"""USDA FoodData Central identity-only request boundary for nutrition research.

Only a product name and brand can enter this object. No diary row, serving,
frequency, date, account, or consumption context is available to the client.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

from .credentials import CredentialError, load_usda_api_key
from .nutrition_provider import UsdaFoodDataCentralWorker
from .repository import normalize_text


@dataclass(frozen=True)
class ProductIdentity:
    name: str
    brand: str = ""

    def __post_init__(self):
        if not self.name.strip() or len(self.name) > 180 or len(self.brand) > 120:
            raise ValueError("Invalid USDA product identity")
        if any(char in self.name + self.brand for char in "\r\n\x00"):
            raise ValueError("Invalid USDA product identity")
        # Reject likely diary annotations accidentally embedded in an identity
        # field rather than transmitting them as if they were product labels.
        if re.search(r"\b(?:19|20)\d{2}[-/]\d{1,2}[-/]\d{1,2}\b|\S+@\S+\.\S+",
                     self.name + " " + self.brand):
            raise ValueError("Identity contains private context")


def _added_sugar_per_100g(food: dict) -> float | None:
    """Accept only an explicitly named added-sugars nutrient in grams."""
    values = []
    for row in food.get("foodNutrients") or ():
        if not isinstance(row, dict):
            continue
        nutrient = row.get("nutrient") if isinstance(row.get("nutrient"), dict) else {}
        name = normalize_text(str(nutrient.get("name") or row.get("nutrientName") or ""))
        unit = str(nutrient.get("unitName") or row.get("unitName") or "").casefold()
        amount = row.get("amount", row.get("value"))
        if (name in {"sugars added", "added sugars"} and unit == "g"
                and isinstance(amount, int | float) and not isinstance(amount, bool)
                and math.isfinite(amount) and 0 <= amount <= 100):
            values.append(float(amount))
    return values[0] if values and len(set(values)) == 1 else None


def _generic_patterns(food: dict, name: str) -> list[dict]:
    if str(food.get("dataType", "")).casefold() == "branded":
        return []
    # A USDA category can establish ingredient identity, but not that a mixed
    # dish contains a meaningful vegetable/fruit/legume/fish amount.
    tokens = set(name.split())
    if len(name.split()) > 5 or tokens & {"pizza", "soup", "sauce", "taco", "tacos",
                                         "sandwich", "burger", "seasoned", "fried",
                                         "breaded", "juice", "shake"}:
        return []
    category = normalize_text(str(food.get("foodCategory", "")))
    names = []
    if "vegetable" in category:
        names.append("vegetable")
    elif "fruit" in category:
        names.append("fruit")
    elif "legume" in category:
        names.append("legume")
    elif "nut and seed" in category:
        names.append("nut_seed")
    elif "finfish" in category or "shellfish" in category:
        names.append("seafood")
        if any(word in name.split() for word in ("salmon", "sardines", "trout", "mackerel")):
            names.append("fatty_fish")
    return [{"pattern_key": key, "presence_class": "meaningful",
             "plant_identity": name.split()[0] if key in {"vegetable", "fruit", "legume", "nut_seed"} else None,
             "provenance": "USDA_exact_generic_category", "confidence": "high",
             "evidence_basis": f"Exact USDA generic identity; category: {category}"} for key in names]


class RestrictedUsdaClient:
    """Review one public food identity. Methods cannot accept private context."""

    def __init__(self, worker: UsdaFoodDataCentralWorker):
        self._worker = worker

    def review(self, identity: ProductIdentity) -> dict:
        name = normalize_text(identity.name)
        brand = normalize_text(identity.brand)
        payload = self._worker._request(
            "POST", "/foods/search",
            # USDA's search endpoint can reject punctuation such as "w/" with
            # HTTP 400. Normalization retains only the approved identity words.
            json={"query": " ".join(part for part in (brand, name) if part),
                  "dataType": ["Branded"] if brand else ["Foundation", "SR Legacy"],
                  "pageSize": 20},
        )
        foods = payload.get("foods")
        if not isinstance(foods, list):
            return {"outcome": "malformed_search"}
        exact = [food for food in foods if isinstance(food, dict) and
                 self._worker._identity_matches(food, name, brand)]
        if len(exact) != 1:
            return {"outcome": "ambiguous_identity" if exact else "no_exact_identity"}
        fdc_id = exact[0].get("fdcId")
        if not isinstance(fdc_id, int) or fdc_id <= 0:
            return {"outcome": "missing_fdc_id"}
        detail = self._worker._request("GET", f"/food/{fdc_id}")
        if not self._worker._identity_matches(detail, name, brand):
            return {"outcome": "detail_identity_mismatch"}
        if detail.get("fdcId") != fdc_id:
            return {"outcome": "detail_id_mismatch"}
        return {"outcome": "exact_identity", "fdc_id": fdc_id,
                "added_sugar_per_100g": _added_sugar_per_100g(detail),
                "patterns": _generic_patterns(detail, name),
                "data_type": str(detail.get("dataType", "")),
                "publication_date": str(detail.get("publicationDate") or ""),
                "modified_date": str(detail.get("modifiedDate") or "")}


def configured_restricted_client() -> RestrictedUsdaClient | None:
    try:
        key = load_usda_api_key()
    except CredentialError:
        return None
    return RestrictedUsdaClient(UsdaFoodDataCentralWorker(key, timeout=8)) if key else None
