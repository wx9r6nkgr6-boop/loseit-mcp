# Nutrition metrics and food patterns: implementation report

2026-09-25, `America/New_York`. This report records the implementation requested after the [governing specification](NUTRITION_METRICS_AND_FOOD_PATTERNS_SPEC.md). Values below are a snapshot of the local nutrition repository on this date; current and historical diary days have unknown completion state unless Lose It explicitly marked them complete. Status thresholds are product display rules, not clinical diagnoses.

## Delivered behavior and boundaries

`metric_engine.py` defines reusable periods, metric definitions, status rules, eligible-day selection, comparisons, confidence, and a JSON-ready result. The Streamlit dashboard and sanitized static iCloud dashboard consume that result. Current Week is Monday through today; Last Week is the preceding Monday–Sunday; Rolling 30 Days is a true trailing 30 calendar days. A partially logged current day is excluded from status averages; missing days are never treated as zero. Earlier logged days without a completion assertion are included provisionally and labelled as such.

Protein uses 150 g/day as the central target and 130 g/day as the On Track floor, with no additional reward above 150. Fiber uses the 28 g/day reference. Total Sugar is the third default prominent card and is **informational**. Added Sugar has its own evidence and coverage gate and never inherits a total-sugar limit. Saturated Fat and Sodium have metric-specific upper-limit bands. Fat/carbohydrate energy shares, cholesterol, weight, and calories remain informational for the reasons below. The default pins are derived from stable metric definitions, without a three-card architectural limit. Pin/hide/reorder controls are future work.

`food_patterns.py` uses append-only, versioned assertions tied to stable canonical food and formulation identities. It stores source and confidence, category and vegetable subgroup, meaningful or directional contribution, optional exact amount, and user-confirmed override precedence. Unknown or ambiguous mixed-dish quantities remain unknown. The plant-variety list contains meaningful, auditable plant identities rather than trace ingredients; its count is exploratory, not a clinical score. The initial pass made 13 assertions across 11 canonical foods. All measured pattern categories have low classification coverage and are explicitly marked **Incomplete** in the final UI, even when a few positive directional observations can be shown. No verified serving or ounce total was claimed from this evidence.

The normal daily update runs deterministic classification and computes all three metric periods before refreshing publication. The separate weekly LaunchAgent runs **Sunday at 11:30 AM local time** under the existing update lock. It is local-only: it reviews new/ambiguous foods, applies conservative deterministic evidence, persists an audit fingerprint, and can safely run twice without duplicate evidence. Research-provider code supports a future exact-match USDA run, but the installed scheduler never invokes it. An automatic approval review rejected a proposed live USDA run because it would transmit private food names, brands, and occurrence counts to USDA. No external research run was made after that rejection. Explicit approval for that data egress is required before enabling it.

## Real-data before and after

Pre-migration backup: `/tmp/loseit-nutrition-pre-metrics.sqlite3`, SHA-256 `3be4dba9b876897ee33709c3d8db7f72dd3832be5922b17ed9abcf2625bd9118`. The schema-8-to-9 migration was tested on a copy before live deployment. SQLite integrity was `ok` before and after. The backup is a local rollback source; SQLite tables were added rather than replacing source data.

| Item | Before | After | Check |
|---|---:|---:|---|
| Raw diary snapshots | 280 | 280 | All rows exactly equal; SHA-256 `32e25ecfd3a9da4fcd7724080cf62586c45551b6b990f0820a96633577021cfb` both times |
| Raw weight snapshots | 16 | 16 | All rows exactly equal; SHA-256 `423ee2485fe0b13faa8d6759d844684a4fe0cf57e4dc8ea89d1f21403f0c3f9c` both times |
| Current normalized occurrences | 1,262 | 1,262 | All rows exactly equal; SHA-256 `fadf01476c9438e1cdb633b21c66fa5cc99ab5692f5d95db6ed481b8a091094b` both times |
| Source food IDs | 266 | 266 | Distinct-ID count |
| Canonical foods | 265 | 265 | Count |
| Formulations | 280 | 280 | Count |
| Food-pattern assertions | 0 | 13 | 11 canonical foods |
| Added-sugar evidence | 0 | 4 | Four food identities; pilot history: 1 run |
| Weekly audit records | 0 | 1 | Manual run and immediate repeat created one record total |
| Historical anomaly findings | 26 | 26 | Preserved, not presented as 26 user tasks |
| Invalid source nutrient fields | 21 | 21 | Preserved |
| Actionable derived research backlog | 0 | 0 | No new action required |
| Needs Your Help | 0 | 0 | No new action required |

Pre-change raw ID digests were `13ef065a783a40b1b4eb3baefdefab1260768225c5e4f14e3565a778befa8143` for diary snapshots, `be6d11bad1cdb2318dcc6f5ff04357be767793116f72c53254f0ed8aa2a2db2d` for weight snapshots, and `f030e9d3f0f7add529b86e4b0fe636c3260ab82cddb8c31cc6ba3e2f1ba617ae` for occurrences. Row-by-row post-change equality is stronger than count or ID equality alone.

## Nutrient validation

Values are daily averages except macro/saturated-fat percentages, which are shares of actual consumed energy. Each column uses only eligible logged days: **4 of 5** Current Week, **7 of 7** Last Week, and **29 of 30** Rolling 30 Days. All included days are provisional because completion is unknown. “Verified” means source nutrient values throughout the eligible observations, not that every day is complete. “Mostly reliable” means some values came from the resolved library. “Incomplete” is shown where a prerequisite, such as the weight unit or added-sugar coverage, is missing.

| Metric | Current Week | Last Week | Rolling 30 Days | Quality / interpretation |
|---|---|---|---|---|
| Protein, g/day | 98.22 — Needs Attention | 117.28 — Slightly Off Track | 110.97 — Needs Attention | Source: 29/29, 58/58, 227/234; remaining 7 rolling values estimated |
| Fiber, g/day | 21.14 — Slightly Off Track | 21.86 — Slightly Off Track | 22.78 — Slightly Off Track | Mostly reliable: estimated 1, 6, 19 occurrences |
| **Total Sugar**, g/day | 96.28 — Informational | 83.51 — Informational | 88.44 — Informational | Mostly reliable: estimated 1, 8, 25; no added-sugar inference |
| Saturated Fat, % kcal | 10.45 — Slightly Off Track | 14.79 — Significantly Off Track | 13.64 — Significantly Off Track | Source/estimated occurrences: 29/0, 53/5, 216/18 |
| Sodium, mg/day | 2,793.56 — Needs Attention | 3,412.24 — Significantly Off Track | 3,226.91 — Significantly Off Track | Source/estimated: 29/0, 56/2, 221/13 |
| Total Fat, % kcal | 31.10 — Informational | 36.66 — Informational | 35.16 — Informational | Contextual macro range, unscored |
| Carbohydrates, % kcal | 50.79 — Informational | 43.94 — Informational | 47.39 — Informational | Contextual macro range, unscored |
| Cholesterol, mg/day | 285.01 — Informational | 252.93 — Informational | 240.64 — Informational | Estimated occurrences: 1, 11, 30 |
| Calories, kcal/day | 1,812.50 — Informational | 2,253.58 — Informational | 2,181.45 — Informational | Intake only; actual adjusted allowance unavailable |
| Weight Trend | 196.58 — Informational | 197.75 — Informational | 196.58 — Informational | Mean of trailing seven days at each period end; source unit unconfirmed, so quality Incomplete and rate withheld |
| Added Sugar | Unavailable; 27.6% occurrence / 12.6% calorie coverage | Unavailable; 22.4% / 10.8% | Unavailable; 22.2% / 10.4% | Incomplete; both required coverage measures are below 90% |

Nutrient occurrence values are covered by source or resolved estimates for all eligible occurrences. Coverage and provenance are still presented separately; a 100% combined value does not imply 100% original-source confidence. Comparable previous periods are included in each result. The current-week comparison uses the same elapsed Monday–weekday portion of the previous week.

### Added-sugar pilot

The pilot examined 30 higher-impact identities and stored four defensible zero-value assertions: two generic unbranded plain-food zeros applicable to matching history, plus two current exact-product manufacturer-label zeros that apply only from 2026-09-25. The [Great Value broccoli listing](https://www.walmart.com/ip/735585383) and [Dymatize Cocoa Pebbles label](https://dymatize.com/products/iso100-cocoa-pebbles) support the current-label assertions. [USDA plain-food guidance](https://aglab.ars.usda.gov/projects-nutrition-corner/take-healthy-eating-challenge) supports the generic identities. The current Nature Valley label was *not* stored because the logged product/portion did not match reliably. Across all history, evidenced occurrences are 204/1,262 (16.2%) and represent 6.7% of source calories. The rolling-30-day gate result is 22.2% of occurrences and 10.4% of calories, far below the required **90% and 90%**. A known subtotal of zero applies only to the covered subset; the total added-sugar intake is unknown. Total Sugar stays the third default card.

### Actual Lose It calorie allowance investigation

Existing read-only GWT-RPC methods, bundled schemas, stored diary responses, and goal/account fixtures were inspected. `DailyLogGoalsState` contains a goal-like fixture value; `DailyBudget`, `DailyBudgetContext`, and `LogEntriesAndGoalsData` schemas also exist. None establishes the actual final activity-adjusted daily allowance shown in Lose It, and none of the 280 stored diary payloads has a validated final allowance field. No new Lose It protocol call or write capability was added. Calories therefore show actual intake with no adherence status. The derived allowance table is ready for future evidence, but scoring requires explicit `loseit_validated_final` provenance and sums each eligible day’s actual final allowance rather than multiplying a fixed goal. A future read-only capture path must be validated against the displayed Lose It value before activation.

## Food-pattern validation

Entries are **meaningful occurrences + additional directional-only occurrences**, never servings or ounces. Zero means no *classified* positive occurrence; the large unknown portion prevents interpreting it as proven absence. All nonempty categories still have **Incomplete** quality because category classification covers under half of eligible occurrences. Plant Variety is the number of distinct qualifying plants; its underlying identity list is kept in the result.

| Pattern | Current Week | Last Week | Rolling 30 Days | Evidence |
|---|---:|---:|---:|---|
| Vegetables | 1 + 0 | 2 + 0 | 11 + 2 | Directional; broccoli classified dark-green |
| Fruit | 0 + 0 | 0 + 0 | 0 + 0 | Unknown classification coverage |
| Fish / Seafood | 1 + 0 | 1 + 0 | 2 + 1 | Directional; salmon/shrimp distinguished |
| Fatty Fish | 1 + 0 | 1 + 0 | 2 + 0 | Directional; shrimp is not promoted to fatty fish |
| Whole Grains | 1 + 0 | 1 + 0 | 4 + 0 | Directional; exact identity required |
| Legumes | 0 + 0 | 2 + 0 | 3 + 0 | Directional when identified |
| Nuts / Seeds | 0 + 0 | 0 + 0 | 0 + 0 | Unknown classification coverage |
| Plant Variety | 2 distinct | 3 distinct | 4 distinct | Exploratory; auditable qualifying-food list |

The 30-day classified counts are 13/234 vegetable, 3/234 seafood, 4/234 whole grain, and 3/234 legume occurrences. Ambiguous foods, mixed dishes, and trace ingredients are not forced into positive categories. The separate “meaningful” and “directional-only” fields can later accept verified quantity where portion evidence supports it.

## Numbered implementation-area record

Each item states disposition; code/docs touched; schema effect; runtime/real-data result; verification; and limitation/follow-up. “No direct schema change” means the area consumes the additive schema-9 foundation.

1. **Period engine — delivered.** Files: `metric_engine.py`, `tests/test_metric_patterns.py`. Schema: no direct change. Runtime/result: the three shared New York calendar periods select 4/5, 7/7, and 29/30 eligible days; current partial day excluded. Verification: boundary, missing-day, and comparison tests plus live results. Limitation/follow-up: unknown completion remains provisional until explicit source evidence exists.
2. **Metric definitions — delivered.** Files: `metric_engine.py`, both dashboards, tests. Schema: no direct change. Runtime/result: 11 stable IDs with semantic type, unit, aggregation, targets/references, prominent order, and evidence version. Verification: definition/status tests and both renderers. Limitation/follow-up: future user pin settings need a separate UI/storage layer.
3. **Status engine — delivered.** Files: `metric_engine.py`, tests. Schema: no direct change. Runtime/result: five graduated labels where supported; protein 130/150, fiber, sodium, and saturated fat scored; sugar and unvalidated calories informational. Verification: threshold and incomplete-data tests; live table above. Limitation/follow-up: product bands are not medical thresholds.
4. **Nutrients dashboard — delivered.** Files: `dashboard.py`, `publication.py`, `metric_engine.py`. Schema: no direct change. Runtime/result: shared selector, Protein/Fiber/Total Sugar top cards, secondary metrics and quality; live table above. Verification: tests, static export inspection. Limitation/follow-up: Added Sugar awaits coverage and Home customization remains future work.
5. **Period comparisons — delivered.** Files: `metric_engine.py`, dashboards, tests. Schema: no direct change. Runtime/result: current-week elapsed equivalent, previous completed week, preceding 30 days; directional prior values in result. Verification: comparison tests. Limitation/follow-up: no significance claim.
6. **Added-sugar pilot — completed conservatively.** Files: `food_patterns.py`, `metric_engine.py`, `repository.py`, tests, spec. Schema: append-only evidence and pilot-run tables. Runtime/result: 30 identities reviewed, four zero assertions, 22.2% occurrence/10.4% calorie rolling coverage; unavailable for scoring. Verification: identity, serving, provenance, history-date, and coverage tests. Limitation/follow-up: more exact historical labels are needed; no automatic card promotion.
7. **Lose It allowance — investigated; capture deferred.** Files: `metric_engine.py`, `repository.py`, tests, spec. Schema: allowance observations table. Runtime/result: no validated final allowance, Calories informational; only proven final provenance would score. Verification: schema/provenance/variable-sum tests and stored-response review. Limitation/follow-up: validate a safe internal read-only source against Lose It’s displayed adjusted allowance.
8. **Classification foundation — delivered.** Files: `food_patterns.py`, `repository.py`, tests. Schema: append-only pattern evidence with category, subgroup, identity/formulation, confidence, source, override, and contribution fields. Runtime/result: 13 assertions/11 foods, unknown preserved. Verification: fresh/migrated DB and override/append-only tests. Limitation/follow-up: exact quantities require defensible serving evidence.
9. **Initial classification — delivered conservatively.** Files: `food_patterns.py`, tests. Schema: 13 new evidence rows. Runtime/result: recent broccoli, salmon, shrimp, grains, legumes and other obvious identities classified; mixed dishes left unknown. Verification: real-data categories and examples tested. Limitation/follow-up: most of 234 trailing occurrences remain unclassified for each pattern.
10. **Food Patterns dashboard — delivered.** Files: `dashboard.py`, `publication.py`, `metric_engine.py`, `food_patterns.py`. Schema: no direct change. Runtime/result: eight pattern rows across three periods, reference/context and confidence, without fabricated serving totals. Verification: rendering/export tests and live table. Limitation/follow-up: the UI correctly marks low classification coverage Incomplete.
11. **Plant Variety — delivered conservatively.** Files: `food_patterns.py`, dashboards, tests. Schema: pattern evidence supports qualifying identity. Runtime/result: 2/3/4 distinct meaningful plants and an auditable list. Verification: trace/duplicate/qualifier tests. Limitation/follow-up: it is exploratory, with no imposed 30-plant clinical target.
12. **Daily automation — delivered.** Files: `update.py`, `metric_engine.py`, `food_patterns.py`, tests. Schema: derived pattern evidence can be added, no source mutation. Runtime/result: sync/resolution then deterministic classification and all-period metric refresh before publication. Verification: update reconciliation tests and live daily-compatible computation. Limitation/follow-up: daily job timing remains the existing 10:00 AM schedule.
13. **Weekly deep audit — installed in local-only mode.** Files: `weekly_scheduler.py`, `food_patterns.py`, tests. Schema: append-only weekly audit runs. Runtime/result: Sunday 11:30 AM LaunchAgent loaded; first manual run added one audit record, repeat added none and left pattern evidence at 13. Verification: idempotency/provider-failure/no-raw-mutation tests and live repeat. Limitation/follow-up: external USDA research awaits explicit private-data egress approval.
14. **Static iCloud dashboard — delivered.** Files: `publication.py`, tests. Schema: publication history only. Runtime/result: three period JSON views and responsive HTML with local selector persistence, at the configured iCloud paths. Verification: SHA-256 and privacy scan below. Limitation/follow-up: static state updates on Mac publication, then remains viewable when Mac is off.
15. **Actionable versus historical findings — delivered.** Files: `resolved.py`, dashboards, tests. Schema: no direct change. Runtime/result: 26 preserved historical findings remain separate from 0 actionable/Needs Your Help issues. Verification: live counts and UI tests. Limitation/follow-up: historical records remain available for audit.
16. **Home customization foundation — delivered, UI deferred.** Files: `metric_engine.py`, dashboards, tests. Schema: no direct change. Runtime/result: stable keys, metadata-driven order, >3 possible pins, secondary metrics retained. Verification: prominence tests. Limitation/follow-up: pin/hide/reorder and temporary surfacing controls need future product work.
17. **Workout reuse — boundary documented; app untouched.** Files: `metric_engine.py`, this report/spec. Schema: no direct change. Runtime/result: `read_metric_dashboard(data_dir, period, today=...)` returns JSON-ready definitions/results for a future client. Verification: tests and static serialization. Limitation/follow-up: future workout integration must make its own explicit data-sharing and device deployment decisions.
18. **Real-data validation — complete.** Files: this report, tests. Schema: read-only validation. Runtime/result: all requested nutrient and pattern values appear above with source/estimate/directional/incomplete labels. Verification: live schema-9 engine output and iCloud snapshot. Limitation/follow-up: these are dated snapshots, not complete-day guarantees.
19. **Migration/raw safety — complete.** Files: `repository.py`, migration tests, this report. Schema: 8→9 additive tables/indexes/triggers. Runtime/result: backup saved; raw rows and normalized occurrences exactly unchanged. Verification: fresh schema, upgrade copy, row hashes/equality, SQLite integrity. Limitation/follow-up: restore from backup if rollback is ever required; preserve append-only history.
20. **Testing — complete.** Files: `tests/test_metric_patterns.py`, `tests/test_automation_reconciliation.py`, `tests/test_reconnect_provider.py`. Schema: tested fresh and migrated. Runtime/result: 683 passed, 7 skipped (existing hosted enrollment skips); Ruff and diff whitespace checks clean. Verification: full suite run after final code edits. Limitation/follow-up: live external USDA matching is not tested against private identities after approval rejection.
21. **Security — preserved.** Files: `publication.py`, `weekly_scheduler.py`, tests. Schema: no write-capable Lose It addition. Runtime/result: exact MCP read-only allowlist unchanged, weekly job local-only, static export sanitized. Verification: published files scanned for credentials/auth headers/raw snapshots/research attempts; none found. Limitation/follow-up: any external research enablement needs explicit authorization and review.
22. **Documentation — updated.** Files: governing spec and this report. Schema: schema-9 contract documented. Runtime/result: metric semantics, food evidence, automation, calorie and added-sugar limits, static export, Home/workout boundary recorded. Verification: matched against code and live results. Limitation/follow-up: update both documents when evidence or product decisions change.
23. **Pass report — delivered.** Files: this report. Schema: none. Runtime/result: before/after, three-period tables, added sugar, calorie investigation, job, safety and regressions recorded. Verification: live measurements and published hashes. Limitation/follow-up: the report is point-in-time.
24. **Regression audit — complete.** Files: tests, `metric_engine.py`, `food_patterns.py`, dashboards. Schema: none beyond migration. Runtime/result: no unresolved regression. During implementation, quality initially described a few positive food-pattern classifications as Directional despite very low category coverage; this was corrected to Incomplete while retaining the positive directional evidence. Weight context initially used only dates within the selected period; it now uses the trailing seven days at period end, so early-week context is not artificially sparse. A fixed top-three rendering assumption was removed in favor of metadata-driven pins. Verification: targeted tests, full suite, live re-evaluation and republished output. Prevention: quality/weight/pin regression tests and explicit unknown coverage fields.
25. **Git — complete after commit/push recorded below.** Files: implementation and documentation only. Schema: no Git-stored live database. Runtime/result: see Git record below. Verification: local/remote hash comparison after push. Limitation/follow-up: pre-existing `missing_coverage.json` remains unrelated and untracked.
26. **Device deployment — not applicable.** Files: no workout-app files. Schema: none. Runtime/result: iPhone N/A; iPad N/A; Apple Watch N/A. Verification: changed-file list contains no workout app. Limitation/follow-up: future integration is a separate task.

## Publication and security verification

Latest published HTML: `~/Library/Mobile Documents/com~apple~CloudDocs/Nutrition Dashboard/nutrition_dashboard.html`, SHA-256 `fc9e70a88a4ac2cfe7a7a2e48ce4e169ee757b6b19d6bd43a2269960aab2de9a`. Latest JSON: `nutrition_snapshot.json`, SHA-256 `7105996759519543efcc8f09544289e720c2915ef5fba551a2ae3b50afcebf10`. Both published at `2026-09-25T19:15:34Z`. The final files have all three `metric_views`, the HTML has the period selector and responsive media rule, and the scan found no `liauth`, API key, authorization header, cookie setter, bearer token, refresh/access token, password, raw diary/weight snapshot, or research-attempt field. `sources/`, raw Lose It files, and the workout app were not modified.

## Git record

Branch: `feat/read-only-nutrition-repository`. Commit, push status, remote/local hash match, and final tree state are recorded in the task response after the final commit. The pre-existing untracked `missing_coverage.json` is intentionally excluded.
