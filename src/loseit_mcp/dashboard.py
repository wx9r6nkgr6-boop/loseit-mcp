"""Streamlit presentation: nutrition, insights and review rules stay in reusable services."""

import argparse
import json
import os
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st

from loseit_mcp.analytics import period_dates, read_analytics
from loseit_mcp.coverage import read_coverage
from loseit_mcp.insights import trend_series
from loseit_mcp.proposals import (
    ProposalError,
    flag_source,
    import_proposal,
    list_proposals,
    parse_proposal,
    proposal_preview,
    read_settings,
    review_context,
    review_proposal,
    save_settings,
)
from loseit_mcp.research_queue import read_research_queue
from loseit_mcp.theme import chart_config, css, load_theme
from loseit_mcp.update import latest_update, run_update

THEME = load_theme()
COLORS = THEME["colors"]

SECTIONS = [
    "Overview",
    "Trends",
    "Meals",
    "Foods",
    "Weight",
    "Coverage & Data Quality",
    "Review & Enrichment",
    "Settings & Targets",
]
LABELS = {
    "calories": "Calories",
    "protein_g": "Protein",
    "carb_g": "Carbohydrates",
    "total_fat_g": "Total fat",
    "saturated_fat_g": "Saturated fat",
    "fiber_g": "Fiber",
    "sugar_g": "Sugar",
    "sodium_mg": "Sodium",
    "cholesterol_mg": "Cholesterol",
}
PRESETS = {
    "Last 7 days": "last7",
    "Last 14 days": "last14",
    "Last 30 days": "last30",
    "Current month": "month",
    "YTD": "ytd",
    "Custom": "custom",
}


def number(value, suffix=""):
    return "Unavailable" if value is None else f"{value:,.1f}{suffix}"


def table(rows):
    if rows:
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
    else:
        st.caption("No records in this view.")


def apply_action(callback, message):
    try:
        callback()
    except ProposalError as exc:
        st.error(str(exc))
        return
    except Exception:  # noqa: BLE001 - UI privacy boundary; never log diary-bearing exceptions
        st.error(
            "The local operation was not completed. Refresh and verify the proposal or database."
        )
        return
    st.session_state["notice"] = message
    st.rerun()


def overview(report):
    logging = report["logging_completeness"]
    cards = st.columns(4)
    cards[0].metric("Logged days", f"{logging['logged_days']} / {logging['calendar_days']}")
    cards[1].metric(
        "Calories / logged day",
        number(report["period_averages"]["calories"]["average_per_logged_day"]),
    )
    cards[2].metric(
        "Protein / complete day",
        number(report["period_averages"]["protein_g"]["average_per_complete_logged_day"], " g"),
    )
    cards[3].metric("Foods needing research", report["data_quality"]["research_queue_count_global"])
    st.caption("Research queue is repository-wide. Other metrics use the selected period.")
    if logging["missing_days"]:
        st.warning(
            "Missing-log days are not zero intake. Calendar-day intake means and incomplete comparisons remain unavailable."
        )
    nutrients = ["carb_g", "total_fat_g", "fiber_g", "sugar_g", "sodium_mg"]
    columns = st.columns(5)
    for column, nutrient in zip(columns, nutrients, strict=True):
        metric = report["period_averages"][nutrient]
        column.metric(
            LABELS[nutrient] + " / complete day",
            number(
                metric["average_per_complete_logged_day"],
                " mg" if nutrient.endswith("_mg") else " g",
            ),
        )
        column.caption(
            f"{metric['complete_logged_days']} complete days; {number(metric['combined_coverage_pct'])}% occurrence coverage"
        )
    protein = report["period_averages"]["protein_g"]
    st.caption(
        f"Protein mean includes {protein['complete_logged_days']} complete-protein days, not every calendar day."
    )
    more = st.columns(4)
    weight = report["weight_summary"]
    more[0].metric(
        "Latest recorded weight", number(weight["latest"]["weight"] if weight["latest"] else None)
    )
    more[1].metric("Weight observations", weight["observation_count"])
    more[2].metric(
        "Foods with review flags", report["food_contributors"]["manual_review_food_count"]
    )
    more[3].metric(
        "Complete macro calorie coverage",
        number(report["data_quality"]["complete_macro_calorie_pct"], "%"),
    )
    if weight["unit"] is None:
        st.caption("Weight unit unavailable from source")
    st.subheader("Factual observations")
    st.caption("Deterministic observations, not dietary or medical advice.")
    for insight in report["insights"]:
        st.info(insight)


def trends(report):
    nutrient = st.selectbox("Metric", list(LABELS), format_func=LABELS.get)
    series = trend_series(report, nutrient)
    frame = pd.DataFrame(series)
    base = alt.Chart(frame).encode(
        x=alt.X("date:T", title="Local date"),
        tooltip=[
            "date:T",
            "status:N",
            "complete_total:Q",
            "partial_known_total:Q",
            "rolling_7:Q",
            "coverage_pct:Q",
        ],
    )
    complete = base.mark_line(point=True, color=COLORS["accentSecondary"]).encode(
        y=alt.Y("complete_total:Q", title=LABELS[nutrient]), detail="complete_segment:N"
    )
    rolling = base.mark_line(color=COLORS["accentPrimary"], strokeDash=[5, 3]).encode(
        y="rolling_7:Q", detail="rolling_segment:N"
    )
    partial = base.mark_point(color=COLORS["warning"], shape="diamond", size=80).encode(
        y="partial_known_total:Q"
    )
    st.altair_chart(complete + rolling + partial, width="stretch")
    st.caption(
        "Cyan: complete daily totals. Amber diamonds: partial known totals, never treated as complete. Pink dashed: seven consecutive complete days. Missing dates break the lines."
    )
    comparison = report.get("comparison", {})
    if comparison:
        st.caption(f"Prior comparison: {comparison['prior_start']} — {comparison['prior_end']}")
        table([{"Metric": LABELS[nutrient], **comparison["metrics"][nutrient]}])
    with st.expander("Daily values and completeness"):
        table(series)


def meals(report):
    rows = []
    for meal, values in report["meal_summary"].items():
        rows.append(
            {
                "Meal (source classification)": meal,
                "Logged date/meal groups": values["logged_meal_days"],
                "Calories": values["calories"]["combined_usable"],
                "Protein g": values["protein_g"]["combined_usable"],
                "Calorie share %": values["calories"]["period_share_pct"],
                "Protein share %": values["protein_g"]["period_share_pct"],
                "Calories / logged meal": values["calories"]["average_per_logged_meal_day"],
                "Protein / logged meal": values["protein_g"]["average_per_logged_meal_day"],
                "Missing calorie occurrences": values["calories"]["missing_count"],
                "Missing protein occurrences": values["protein_g"]["missing_count"],
            }
        )
    table(rows)
    st.caption(
        "A meal occurrence is a distinct logged date/meal group, not one food entry. Shares are withheld when the selected-period nutrient total is incomplete."
    )
    st.altair_chart(
        alt.Chart(pd.DataFrame(rows))
        .mark_bar(color=COLORS["accentSecondary"])
        .encode(
            x="Meal (source classification):N",
            y="Calories:Q",
            tooltip=[
                "Meal (source classification):N",
                "Calories:Q",
                "Missing calorie occurrences:Q",
            ],
        ),
        width="stretch",
    )


def foods(report):
    choices = {
        "All foods": None,
        "Most frequent": "frequency",
        **{
            f"Top {LABELS[n].lower()}": n
            for n in ("calories", "protein_g", "carb_g", "total_fat_g", "sugar_g", "sodium_mg")
        },
    }
    selection = st.selectbox("Food table", list(choices))
    query = st.text_input("Search food name or brand").strip().casefold()
    contributors = report["food_contributors"]
    ranking = choices[selection]
    catalog = contributors["catalog"]
    indices = (
        range(len(catalog))
        if ranking is None
        else contributors["most_frequent"]
        if ranking == "frequency"
        else contributors["rankings"][ranking]
    )
    metric = ranking if ranking not in {None, "frequency"} else "calories"
    status = {
        r["source_food_id"]: r for r in report["data_quality"]["foods"] if r["source_food_id"]
    }
    rows = []
    for index in indices:
        food = catalog[index]
        if query and query not in (food["food_name"] + " " + food["brand"]).casefold():
            continue
        values = food["metrics"][metric]
        quality = status.get(food["source_food_id"], {})
        rows.append(
            {
                "Food": food["food_name"],
                "Brand": food["brand"],
                "Source food ID": food["source_food_id"],
                "Occurrences": food["occurrence_count"],
                "Source contribution": values[0],
                "Enriched contribution": values[1],
                "Combined contribution": values[2],
                "Missing occurrences": values[5],
                "Research status": quality.get("research_queue_status_global", "complete"),
                "Review flags": food["review_flags"],
                "Average logged amount": food["average_logged_amount"],
                "Unit": food["unit"],
            }
        )
    st.caption(
        f"Contribution metric: {LABELS[metric]}. Rankings use known contributions; partial totals retain missing counts. Search filters the selected table."
    )
    table(rows)


def weights(report):
    weight = report["weight_summary"]
    if weight["unit"] is None:
        st.warning("Weight unit unavailable from source")
    else:
        st.caption("Source unit: " + weight["unit"])
    columns = st.columns(4)
    columns[0].metric("Latest", number(weight["latest"]["weight"] if weight["latest"] else None))
    columns[1].metric("Observations", weight["observation_count"])
    columns[2].metric("Minimum", number(weight["min"]))
    columns[3].metric("Maximum", number(weight["max"]))
    if weight["daily"]:
        frame = pd.DataFrame(weight["daily"])
        base = alt.Chart(frame).encode(x="date:T")
        chart = base.mark_point(color=COLORS["accentSecondary"], size=55).encode(
            y=alt.Y("weight:Q", title="Recorded weight (source units)"),
            tooltip=["date:T", "weight:Q"],
        )
        if weight["unit"]:
            chart += base.mark_line(color=COLORS["accentPrimary"]).encode(y="rolling_7:Q")
            chart += base.mark_line(color=COLORS["warning"], strokeDash=[4, 3]).encode(
                y="rolling_30:Q"
            )
        st.altair_chart(chart, width="stretch")
    st.caption(
        "Recorded observations are not interpolated. Unit-dependent statistics and rolling averages stay unavailable when the unit is unknown."
    )
    table(weight["daily"])


def coverage(report, data_dir):
    st.info(
        "Unresolved reference cache does not mean incomplete nutrition. The standard research queue includes only genuine standard-nutrient gaps."
    )
    st.metric(
        "Complete macro calorie coverage",
        number(report["data_quality"]["complete_macro_calorie_pct"], "%"),
    )
    table([{"Nutrient": LABELS[n], **m} for n, m in report["nutrient_coverage"].items()])
    st.subheader("Food-level gaps and review flags")
    table(report["data_quality"]["foods"])
    st.caption(
        "Source-unreliable flags are annotations: historical source numbers are never rewritten or silently removed."
    )
    with st.expander("Unknown nutrient details (repository-wide)"):
        if st.button("Load advanced coverage"):
            advanced = read_coverage(data_dir)
            st.json({n: v for n, v in advanced["nutrients"].items() if n not in LABELS})


def review(data_dir, report):
    queue = read_research_queue(data_dir)
    proposals = list_proposals(data_dir)
    st.caption(
        f"{queue['queue_count']} foods still have standard-nutrient research gaps. Update My Nutrition Data checks available evidence; only exceptions need your attention."
    )
    with st.expander("Advanced · manual proposal import"):
        uploaded = st.file_uploader("Local researched proposal JSON", type=["json"])
        if st.button("Import as pending proposal", disabled=uploaded is None):
            apply_action(
                lambda: import_proposal(data_dir, uploaded.getvalue()),
                "Proposal imported or recognized as an existing duplicate; no nutrition approved.",
            )
    subview = st.selectbox(
        "Review view",
        [
            "Needs Review",
            "Automatically Enriched",
            "Approved",
            "Rejected / Deferred",
            "History",
            "Needs Research",
            "Ready to Approve",
        ],
    )
    if subview == "Needs Research":
        rows = queue["foods"]
        if rows:
            chosen = st.selectbox(
                "Research food",
                range(len(rows)),
                format_func=lambda i: rows[i]["food_name"] + " · " + rows[i]["brand"],
            )
            st.json(rows[chosen])
        else:
            st.success("No missing standard-nutrient research items.")
        return
    if subview == "Needs Review":
        flagged = [
            f for f in report["data_quality"]["foods"] if f["review_flags"] and f["source_food_id"]
        ]
        st.subheader("Contradictory or manually flagged source records")
        st.caption(
            "This list uses the selected period. Choose YTD to inspect all current 2026 flags."
        )
        if flagged:
            chosen = st.selectbox(
                "Source record to review",
                range(len(flagged)),
                format_func=lambda i: flagged[i]["food_name"] + " · " + flagged[i]["brand"],
            )
            source = flagged[chosen]
            st.warning("Problem: " + ", ".join(str(f) for f in source.get("review_flags", [])))
            if "oatnut" in source["food_name"].lower():
                st.warning(
                    "Confirm the actual slice count and label: most stored values correspond to four slices while sodium corresponds to two. Missing protein must not be filled automatically."
                )
            with st.expander("Stored portion and source evidence"):
                st.json(review_context(data_dir, source["source_food_id"]))
            with st.form("source_flag"):
                status = st.selectbox(
                    "Source review status", ["unreliable", "needs_review", "cleared"]
                )
                reason = st.text_area("Reason for source flag")
                if st.form_submit_button("Record source review flag"):
                    apply_action(
                        lambda: flag_source(data_dir, source["source_food_id"], status, reason),
                        "Source review annotation recorded; source values unchanged.",
                    )
        else:
            st.caption(
                "No source food IDs in this period. Choose another period to review a source record."
            )
    mapping = {
        "Needs Review": {"needs_review", "ready", "partially_approved"},
        "Automatically Enriched": {"approved"},
        "Ready to Approve": {"ready", "partially_approved"},
        "Approved": {"approved"},
        "Rejected / Deferred": {"rejected", "deferred"},
        "History": None,
    }
    filtered = [p for p in proposals if mapping[subview] is None or p["status"] in mapping[subview]]
    if subview in {"Automatically Enriched", "Approved"}:
        filtered = [
            p
            for p in filtered
            if any(a.get("actor") == "policy" and a["action"] == "approved" for a in p["history"])
            == (subview == "Automatically Enriched")
        ]
    if not filtered:
        st.caption("No proposals in this view.")
        return
    index = st.selectbox(
        "Proposal",
        range(len(filtered)),
        format_func=lambda i: (
            f"#{filtered[i]['id']} · "
            + filtered[i]["document"]["food_name"]
            + " · "
            + filtered[i]["status"]
        ),
    )
    proposal = filtered[index]
    doc = proposal["document"]
    for action in proposal["history"][-1:]:
        if action["action"] == "needs_review":
            st.warning(action["note"])
    if doc.get("mismatch_explanation"):
        st.warning(doc["mismatch_explanation"])
    if proposal["context_problem"]:
        st.warning(proposal["context_problem"])
    if proposal["conflicting_proposals"]:
        st.warning(
            "Conflicting pending proposals exist for this food. Review both before acknowledging approval."
        )
    left, right = st.columns(2)
    try:
        preview = proposal_preview(data_dir, doc)
        with left:
            st.subheader("Stored source")
            for variant in preview["source"]["portion_variants"]:
                st.write(variant["logged_portion"])
                table(
                    [
                        {"Nutrient": LABELS.get(n, n), "Source value": v}
                        for n, v in variant["source_nutrients"].items()
                    ]
                )
        with right:
            st.subheader("Proposed research")
            st.text(doc["source_reference"])
            st.link_button("Open research reference", doc["reference_url"])
            st.caption(doc["nutrition_basis"]["description"])
            table(
                [
                    {
                        "Nutrient": LABELS[n["nutrient"]],
                        "Value": n.get("estimated_value"),
                        "Unit": n["unit"],
                    }
                    for n in doc["nutrients"]
                ]
            )
            st.caption(f"Confidence: {doc['confidence']} · researched {doc['research_date']}")
            st.text(doc["assumptions"])
        st.subheader("Proposed nutrient effects")
        table(preview["changes"])
    except ProposalError:
        st.warning(
            "Source context is no longer available. This proposal can be deferred or rejected."
        )
    with st.expander("Proposal and decision history", expanded=subview == "History"):
        st.json(proposal["history"])
    if proposal["status"] in {"approved", "rejected"}:
        return
    st.caption(
        "Approvals never overwrite source values. Low-confidence references remain labelled and may not be usable in analytics. Numeric reuse requires a verified portion mapping."
    )
    with st.expander("Advanced · edit evidence or portion mapping"):
        edited_text = st.text_area(
            "Edit proposal JSON before approval (optional)",
            json.dumps(doc, indent=2),
            height=280,
            key=f"edit_{proposal['id']}",
        )
    try:
        edited = parse_proposal(edited_text.encode())
        valid = True
    except ProposalError as exc:
        edited, valid = doc, False
        st.error(str(exc))
    if "occurrence_scaling" not in edited["nutrition_basis"]:
        st.info(
            "To use these values in totals, confirm how the label portion maps to your logged unit. Leave this unchecked if unsure, and defer the item."
        )
        units_per_label = st.number_input(
            "Logged units represented by the label values",
            min_value=0.01,
            max_value=100.0,
            value=float(edited["nutrition_basis"]["amount"]),
            key=f"portion_amount_{proposal['id']}",
        )
        if st.checkbox(
            "I confirmed this portion mapping from the label and my log",
            key=f"portion_confirm_{proposal['id']}",
        ):
            edited["nutrition_basis"]["amount"] = units_per_label
            edited["nutrition_basis"]["occurrence_scaling"] = {
                "method": "logged_amount",
                "unit": edited["nutrition_basis"]["unit"],
            }
            edited["assumptions"] += (
                f" User confirmed label corresponds to {units_per_label:g} logged units."
            )
    options = [
        n["nutrient"]
        for n in edited["nutrients"]
        if n["nutrient"] not in proposal["approved_nutrients"]
    ]
    selected = st.multiselect(
        "Nutrients to approve", options, default=options, key=f"nutrients_{proposal['id']}"
    )
    note = st.text_area("Review note", key=f"note_{proposal['id']}")
    acknowledged = st.checkbox(
        "I reviewed any conflicting proposals", key=f"conflict_{proposal['id']}"
    )
    confirmed = st.checkbox(
        "I verified the product, evidence, units and portion basis for the selected nutrients",
        key=f"confirm_{proposal['id']}",
    )
    buttons = st.columns(3)
    if buttons[0].button(
        "Approve selected nutrients",
        disabled=not valid or not confirmed or not selected,
        type="primary",
    ):
        apply_action(
            lambda: review_proposal(
                data_dir,
                proposal["id"],
                "approve",
                selected=selected,
                edited_document=edited,
                note=note,
                acknowledge_conflicts=acknowledged,
            ),
            "Reviewed enrichment saved; analytics and research queue refreshed.",
        )
    if buttons[1].button("Reject proposal"):
        apply_action(
            lambda: review_proposal(data_dir, proposal["id"], "rejected", note=note),
            "Rejection recorded; no nutrition values changed.",
        )
    if buttons[2].button("Defer / needs more research"):
        apply_action(
            lambda: review_proposal(
                data_dir,
                proposal["id"],
                "deferred",
                edited_document=edited if valid else None,
                note=note,
            ),
            "Deferral recorded with history preserved.",
        )


def settings_view(data_dir, settings, report):
    st.caption(
        "Optional personal analysis targets. Empty means not configured. These controls do not provide recommendations."
    )
    with st.form("targets"):
        values = {}
        for key, label in [
            ("protein_target", "Protein target (g)"),
            ("calorie_min", "Calorie minimum"),
            ("calorie_max", "Calorie maximum"),
            ("fiber_target", "Fiber target (g)"),
            ("sodium_limit", "Sodium upper target (mg)"),
            ("sugar_target", "Sugar upper target (g)"),
        ]:
            values[key] = st.number_input(
                label,
                min_value=0.0,
                max_value=1000000.0,
                value=float(settings[key]) if settings[key] is not None else None,
                placeholder="Not configured",
            )
        if st.form_submit_button("Save local targets"):
            apply_action(lambda: save_settings(data_dir, values), "Local targets saved.")
    st.subheader("Adherence on eligible complete days")
    table(
        [
            {"Metric": key, **value}
            for key, value in report["consistency"].items()
            if isinstance(value, dict)
        ]
    )
    table(
        [{"Metric": key, **value} for key, value in report["additional_target_adherence"].items()]
    )


def main():
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=Path(
            os.environ.get("LOSEIT_DATA_DIR", str(Path.home() / ".local/share/loseit-readonly"))
        ),
    )
    args, _ = parser.parse_known_args()
    st.set_page_config(page_title="Nutrition · Local", page_icon="◈", layout="wide")
    st.markdown(css(THEME), unsafe_allow_html=True)
    alt.theme.register("workout_companion", enable=True)(lambda: chart_config(THEME))
    st.sidebar.title("Nutrition")
    st.sidebar.caption("WORKOUT COMPANION · LOCAL NUTRITION")
    update_clicked = st.sidebar.button("Update My Nutrition Data", type="primary", width="stretch")
    section = st.sidebar.radio("Navigate", SECTIONS)
    preset = st.sidebar.selectbox("Date range", list(PRESETS))
    period = PRESETS[preset]
    if preset == "Custom":
        default_start, default_end = period_dates(period="last7")
        start = st.sidebar.date_input("Start date", default_start)
        end = st.sidebar.date_input("End date", default_end)
    else:
        start, end = period_dates(period=period)
    grouping = "name" if st.sidebar.checkbox("Group by normalized name + brand") else "id"
    st.sidebar.button("Refresh local data")
    st.sidebar.caption(
        "Browsing and Refresh are local-only. Update explicitly reads Lose It and runs the controlled evidence worker. No background polling."
    )
    if update_clicked:
        with st.status("Updating nutrition data", expanded=True) as status:
            result = run_update(args.data_dir, progress=st.write)
            status.update(
                label="Update stopped" if result["status"] == "failed" else "Update finished",
                state="error" if result["status"] == "failed" else "complete",
                expanded=False,
            )
        st.session_state["update_result"] = result
    result = st.session_state.get("update_result") or latest_update(args.data_dir)
    if result:
        if result["status"] == "failed":
            st.error(result.get("error", "Update stopped; retry when the connection is restored."))
        else:
            st.success(
                f"Data updated through {result['through_date']} · {result['occurrences_added']} new entries · {result['weights_added']} new weights"
            )
            st.caption(
                f"{result['foods_checked']} foods checked · {result['automatically_enriched']} automatically enriched · {result['nutrients_filled']} nutrients filled · {result.get('needs_review', 0)} need review · {result['unresolved']} unresolved · {result['failures']} research failures"
            )
            if result["unresolved"]:
                st.info(result["research_capability"])
    if "notice" in st.session_state:
        st.success(st.session_state.pop("notice"))
    st.title(section)
    st.caption(f"{start:%b %d, %Y} — {end:%b %d, %Y}")
    try:
        settings = read_settings(args.data_dir)
        report = read_analytics(
            args.data_dir,
            start,
            end,
            period=period,
            grouping=grouping,
            include_all_foods=True,
            **settings,
        )
        if section == "Overview":
            overview(report)
        elif section == "Trends":
            trends(report)
        elif section == "Meals":
            meals(report)
        elif section == "Foods":
            foods(report)
        elif section == "Weight":
            weights(report)
        elif section == "Coverage & Data Quality":
            coverage(report, args.data_dir)
        elif section == "Review & Enrichment":
            review(args.data_dir, report)
        else:
            settings_view(args.data_dir, settings, report)
    except ProposalError as exc:
        st.error(str(exc))
    except Exception:  # noqa: BLE001 - UI privacy boundary; never log diary-bearing exceptions
        st.error(
            "Could not complete this local view. Check the database, selected dates or proposal. No remote request was made."
        )


if __name__ == "__main__":
    main()
