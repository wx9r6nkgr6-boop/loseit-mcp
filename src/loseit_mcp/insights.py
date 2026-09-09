"""Reusable factual observations and complete-day chart/adherence projections."""

from statistics import mean


def trend_series(report, nutrient):
    rows = []
    segment = rolling_segment = 0
    history = []
    for day in report["daily_metrics"]:
        source, estimated, known, sc, ec, missing, total = day["nutrients"][nutrient]
        complete = bool(total) and not missing
        value = known if complete else None
        history.append(value)
        rolling = (
            mean(history[-7:])
            if len(history) >= 7 and all(v is not None for v in history[-7:])
            else None
        )
        if not complete:
            segment += 1
        if rolling is None:
            rolling_segment += 1
        rows.append(
            {
                "date": day["date"],
                "source_reported": source,
                "estimated_or_enriched": estimated,
                "complete_total": value,
                "partial_known_total": known if not complete else None,
                "rolling_7": rolling,
                "complete_segment": segment,
                "rolling_segment": rolling_segment,
                "coverage_pct": 100 * (sc + ec) / total if total else None,
                "status": "Complete"
                if complete
                else "Partial total"
                if total
                else "No food logged",
            }
        )
    return rows


def target_adherence(report, targets):
    result = {}
    for key, nutrient, direction in (
        ("fiber_target", "fiber_g", "at_least"),
        ("sodium_limit", "sodium_mg", "at_most"),
        ("sugar_target", "sugar_g", "at_most"),
    ):
        target = targets.get(key)
        values = [
            d["nutrients"][nutrient][2]
            for d in report["daily_metrics"]
            if d["occurrence_count"] and not d["nutrients"][nutrient][5]
        ]
        count = (
            sum(v >= target if direction == "at_least" else v <= target for v in values)
            if target is not None
            else None
        )
        result[nutrient] = {
            "target": target,
            "direction": direction,
            "eligible_complete_days": len(values),
            "target_days": count,
            "target_pct": 100 * count / len(values) if count is not None and values else None,
        }
    return result


def generate_insights(report):
    """No advice, imputation, network or model calls. At most five factual slots."""
    statements = []
    adherence = report["consistency"]["protein_g"]
    if adherence["target_days"] is not None and adherence["eligible_complete_days"]:
        statements.append(
            f"Protein met the configured target on {adherence['target_days']} of {adherence['eligible_complete_days']} complete-protein days."
        )
    comparison = report.get("comparison", {}).get("metrics", {}).get("calories", {})
    change = comparison.get("percentage_change")
    if change is not None:
        statements.append(
            f"Average calories {'increased' if change >= 0 else 'decreased'} {abs(change):.1f}% versus the prior equivalent period; both periods have complete data."
        )
    weekday = report["weekday_weekend"]["weekday"]["nutrients"]["calories"][
        "average_per_calendar_day"
    ]
    weekend = report["weekday_weekend"]["weekend"]["nutrients"]["calories"][
        "average_per_calendar_day"
    ]
    if weekday is not None and weekend is not None and weekday != weekend:
        statements.append(
            f"Weekend calorie averages were {'higher' if weekend > weekday else 'lower'} than weekday averages; both groups have complete data."
        )
    incomplete_fiber = sum(
        d["occurrence_count"] > 0 and d["nutrients"]["fiber_g"][5] > 0
        for d in report["daily_metrics"]
    )
    if incomplete_fiber:
        statements.append(f"Fiber coverage is incomplete on {incomplete_fiber} logged days.")
    protein = report["period_averages"]["protein_g"]
    foods = report["food_contributors"]
    top = foods["rankings"]["protein_g"][:3]
    if protein["complete_for_logged_days"] and protein["combined_usable"] and top:
        contribution = sum(foods["catalog"][i]["metrics"]["protein_g"][2] for i in top)
        statements.append(
            f"The top {len(top)} foods account for {100 * contribution / protein['combined_usable']:.1f}% of recorded protein on logged days."
        )
    logging = report["logging_completeness"]
    statements.append(
        f"Food was logged on {logging['logged_days']} of {logging['calendar_days']} calendar days; missing-log days are not zero intake."
    )
    return statements[:5]
