"""Conservative, read-only nutrition research providers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

import httpx

from .credentials import CredentialError, load_usda_api_key
from .enrichment_worker import CuratedEvidenceWorker, Evidence
from .repository import normalize_text

USDA_API_URL = "https://api.nal.usda.gov/fdc/v1"


@dataclass(frozen=True)
class ResearchIssue(Exception):
    """A sanitized, non-secret provider outcome."""

    status: str
    provider: str
    summary: str
    needs_review: bool = False
    failure: bool = False


NUTRIENT_NAMES = {
    "energy": "calories",
    "protein": "protein_g",
    "carbohydrate, by difference": "carb_g",
    "total lipid (fat)": "total_fat_g",
    "fatty acids, total saturated": "saturated_fat_g",
    "fiber, total dietary": "fiber_g",
    "sugars, total including nlea": "sugar_g",
    "sugars, total": "sugar_g",
    "total sugars": "sugar_g",
    "sodium, na": "sodium_mg",
    "cholesterol": "cholesterol_mg",
}


class UsdaFoodDataCentralWorker:
    """Research exact names in USDA FDC; never guesses a near/generic replacement."""

    provider = "USDA FoodData Central"
    capability = "USDA FoodData Central exact-product research"

    def __init__(self, api_key: str, *, client: Any | None = None, timeout: float = 15.0):
        self._api_key = api_key
        self._client = client
        self._timeout = timeout

    def _request(self, method: str, path: str, **kwargs) -> dict:
        own = self._client is None
        client = self._client or httpx.Client(timeout=self._timeout, trust_env=True)
        try:
            response = client.request(
                method,
                USDA_API_URL + path,
                params={"api_key": self._api_key},
                **kwargs,
            )
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise TypeError("not an object")
            return payload
        except httpx.TimeoutException:
            raise ResearchIssue(
                "timeout", self.provider, "Provider request timed out.", failure=True
            ) from None
        except (TypeError, ValueError):
            raise ResearchIssue(
                "malformed_response",
                self.provider,
                "Provider response was malformed.",
                failure=True,
            ) from None
        except httpx.HTTPError:
            raise ResearchIssue(
                "provider_unavailable",
                self.provider,
                "Provider returned no usable response.",
                failure=True,
            ) from None
        finally:
            if own:
                client.close()

    def research(self, candidate: dict) -> Evidence:
        name = normalize_text(candidate.get("food_name", ""))
        brand = normalize_text(candidate.get("brand", ""))
        query = " ".join(value for value in (candidate.get("brand"), candidate.get("food_name")) if value)
        data_types = ["Branded"] if brand else ["Foundation", "Survey (FNDDS)", "SR Legacy"]
        payload = self._request(
            "POST",
            "/foods/search",
            json={"query": query, "dataType": data_types, "pageSize": 25},
        )
        foods = payload.get("foods")
        if not isinstance(foods, list):
            raise ResearchIssue(
                "malformed_response", self.provider, "Provider response was malformed.", failure=True
            )
        exact = [food for food in foods if self._identity_matches(food, name, brand)]
        if not exact:
            generic_substitution = bool(brand) and any(
                normalize_text(str(food.get("description", ""))) == name
                and str(food.get("dataType", "")).casefold() != "branded"
                for food in foods
            )
            raise ResearchIssue(
                "branded_generic_blocked" if generic_substitution else "no_exact_match",
                self.provider,
                "A generic record was not substituted for the branded food."
                if generic_substitution
                else "No exact product identity was found; no generic substitution was made.",
                needs_review=True,
            )
        selected = self._select(exact)
        detail_rows = {str(food["fdcId"]): food for food in exact}
        fingerprints = {
            tuple(sorted(_nutrients(details).items())) for details in detail_rows.values()
        }
        if len(fingerprints) > 1:
            raise ResearchIssue(
                "conflicting_evidence",
                self.provider,
                "Exact-identity USDA records disagree on nutrient values.",
                needs_review=True,
            )
        details = detail_rows[str(selected["fdcId"])]
        nutrients = _nutrients(details)
        if not nutrients:
            raise ResearchIssue(
                "malformed_response", self.provider, "Matched record had no usable nutrients.", failure=True
            )
        variants = candidate.get("portion_variants") or []
        reconciled = bool(variants) and all(
            row.get("logged_portion", {}).get("unit") == "g" for row in variants
        )
        fdc_id = str(selected["fdcId"])
        return Evidence(
            source_food_id=candidate["source_food_id"],
            food_name=candidate["food_name"],
            brand=candidate["brand"],
            url=f"https://fdc.nal.usda.gov/food-details/{fdc_id}/nutrients",
            title=f"USDA FoodData Central · {selected.get('description', candidate['food_name'])} · FDC {fdc_id}",
            source_kind="authoritative_database",
            research_date=date.today().isoformat(),  # noqa: DTZ011
            nutrients=nutrients,
            basis_amount=100,
            basis_unit="g",
            basis_description="USDA FoodData Central values per 100 g",
            portion_reconciled=reconciled,
            identity_verified=True,
            confidence="high",
            concerns=()
            if reconciled
            else ("USDA's 100 g basis could not be reconciled to the logged serving unit.",),
            match_type="exact_brand_product" if brand else "USDA_or_other_authoritative_generic",
        )

    @staticmethod
    def _identity_matches(food: dict, name: str, brand: str) -> bool:
        if normalize_text(str(food.get("description", ""))) != name:
            return False
        if not brand:
            return str(food.get("dataType", "")).casefold() != "branded"
        if str(food.get("dataType", "")).casefold() != "branded":
            return False
        return brand in {
            normalize_text(str(food.get("brandOwner", ""))),
            normalize_text(str(food.get("brandName", ""))),
        }

    def _select(self, exact: list[dict]) -> dict:
        if len(exact) == 1:
            return exact[0]
        upcs = {str(food.get("gtinUpc", "")) for food in exact}
        if len(upcs) == 1 and "" not in upcs:
            return max(exact, key=lambda row: str(row.get("publicationDate", "")))
        raise ResearchIssue(
            "ambiguous_match",
            self.provider,
            "Multiple exact-name products could not be disambiguated.",
            needs_review=True,
        )


def _nutrients(payload: dict) -> dict[str, float]:
    result: dict[str, float] = {}
    rows = payload.get("foodNutrients") or []
    if not isinstance(rows, list):
        return result
    for row in rows:
        if not isinstance(row, dict):
            continue
        nutrient = row.get("nutrient") if isinstance(row.get("nutrient"), dict) else {}
        name = str(nutrient.get("name") or row.get("nutrientName") or "").casefold()
        unit = str(nutrient.get("unitName") or row.get("unitName") or "").casefold()
        value = row.get("amount", row.get("value"))
        key = NUTRIENT_NAMES.get(name)
        if key and isinstance(value, int | float) and not isinstance(value, bool):
            if key == "calories" and unit not in {"kcal", ""}:
                continue
            result[key] = float(value)
    return result


class ConfiguredResearchWorker:
    """Curated evidence first, then the configured general provider."""

    requires_context = True

    def __init__(self, api_key: str | None, *, provider: Any | None = None):
        self._curated = CuratedEvidenceWorker()
        self._provider = provider or (
            UsdaFoodDataCentralWorker(api_key) if api_key else None
        )
        self.capability = (
            "Verified bundled evidence plus USDA FoodData Central."
            if self._provider
            else "Verified bundled evidence; USDA FoodData Central is not configured."
        )

    def research(self, candidate: dict) -> Evidence:
        evidence = self._curated.research(candidate)
        if evidence is not None and evidence.source_kind in {
            "manufacturer_label",
            "retailer_label",
        }:
            return evidence
        if self._provider is not None:
            try:
                return self._provider.research(candidate)
            except ResearchIssue:
                if evidence is not None:
                    return evidence
                raise
        if evidence is not None:
            return evidence
        raise ResearchIssue(
            "provider_not_configured",
            "USDA FoodData Central",
            "Nutrition research provider is not configured.",
        )


def build_research_worker() -> ConfiguredResearchWorker:
    try:
        key = load_usda_api_key()
    except CredentialError:
        key = None
    return ConfiguredResearchWorker(key)
