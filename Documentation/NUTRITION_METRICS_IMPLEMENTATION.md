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

---

# Customizable Home and restricted USDA follow-up — 2026-09-26

This dated section supersedes the earlier “customization deferred” and “USDA disabled” statements above. The 2026-09-25 measurements remain historical snapshots. The current pass used a new pre-change backup at `/tmp/loseit-home-prepass-20260926.sqlite3`, SHA-256 `42b8e2723e3ebc94ccf8ce61d42ce098c241dd3966a6e88933af212001ed42b3`.

## USDA data-egress audit

The weekly job ranks foods by occurrence count and source calories **inside SQLite**. It constructs `ProductIdentity(name, brand)` and passes only that two-field object to `RestrictedUsdaClient.review`. The client sends a USDA `/foods/search` POST with exactly `query` (normalized brand and product-name words), `dataType`, and fixed `pageSize=20`; it uses the configured USDA API key as the required authentication query parameter. A follow-up USDA `/food/{fdcId}` GET contains only the USDA ID returned by the exact search result. These endpoints and the key requirement are documented in the [USDA FoodData Central API Guide](https://fdc.nal.usda.gov/api-guide/). HTTP requests go only to `https://api.nal.usda.gov/fdc/v1`; the approval does not enable other research providers.

| May leave for USDA | Stays local and is not in the request object |
|---|---|
| Product/food name; brand/manufacturer; USDA-returned FDC ID for detail lookup; USDA API key for authentication | Occurrence counts/frequency; consumption or audit dates; meal names; logged quantities/servings; diary context; accompanying foods; consumed calories/nutrients; source food/canonical IDs; Lose It account and user identifiers; raw payloads |

The request construction is checked with a fake HTTP transport, not merely a fake research provider. Tests assert the exact URL, method, query parameters, JSON keys and body, and reject private-context fields. The provider accepts only exact normalized name and exact brand owner/name for branded records; ambiguous/multiple results, weak names, missing explicit Added Sugars, and unreconciled serving units cannot create evidence. Generic category evidence is recorded only from an exact generic identity. Current branded evidence is date-scoped from the research date. No total-sugar value substitutes for Added Sugar. Locally stored audit summaries retain prioritization counts for traceability, but those summaries are neither sent to USDA nor included in the static export.

### Controlled research and Added Sugar before/after

Thirty recent higher-impact identities were researched on a database copy and then in the live local derived layer. **No exact identity passed** the conservative name/brand requirement, so no added-sugar or food-pattern assertion was created. One USDA HTTP 400 arose from punctuation in a product name; the query now normalizes *only* approved name/brand words. The retry returned a normal no-exact-match result. An immediate repeated weekly run made **zero additional USDA requests** and created no evidence. The live audit history grew from one to three rows: the initial restricted pass, a changed outcome after the provider-error fix, and no duplicate row for the final repeat. This is a useful negative result rather than a reason to loosen matching.

| Measure | Before | After |
|---|---:|---:|
| Canonical foods with Added Sugar evidence | 4 | 4 |
| Added-sugar evidence rows | 4 | 4 |
| Historical occurrences with defensible Added Sugar | 206 / 1,270 (16.2%) | 206 / 1,270 (16.2%) |
| Historical source-calorie coverage | 6.8% | 6.8% |
| Rolling-30-day occurrence coverage | 22.3% | 22.3% |
| Rolling-30-day source-calorie coverage | 10.4% | 10.4% |
| Food-pattern evidence assertions | 13 | 13 |

The four existing Added Sugar assertions are **two high-confidence USDA generic verified zeros** and **two high-confidence current manufacturer-label zeros**. No low-confidence USDA substitute was added. The rolling gate remains **90% of occurrences and 90% of source calories**, so Added Sugar is unavailable for scoring and Total Sugar remains the third default pin. Current branded labels cannot backfill older diary entries.

All 30 reviewed identities were rejected because no exact USDA product identity matched the local name and brand: Slim Fast Coffee House Shake; Great Value Seasoned Fries; Kellogg's Pop-Tarts Chocolate Chip Cookie Dough; Walmart Frosted Sugar Cookie; Nature's Valley Dark Chocolate Crunchy Oats Granola Bar; Great Value Cheese Ravioli; Dymatize Dunkin’ Mocha Latte protein powder; AMC Large Buttered Popcorn; Thomas' 100% Whole Wheat Bagel; Beyond Meat Beyond Burger; Jimmy Dean Pancakes & Sausage on a Stick; Hershey's Dark Chocolate Kisses; Birds Eye frozen chicken stir fry; Stouffer's Chicken & Vegetable Rice Bake; Marketside Double Chocolate Iced Cake; North Italia White Truffle Garlic Bread; Great Value Chicken Alfredo; Oreo Frozen Dessert Mini Cones; Oscar Mayer Beef & Pork Franks (logged brand typo); Banquet Salisbury Steak with Brown Gravy; Marketplace Chocolate Brioche; Red Lobster Cheddar Bay Biscuit; Red Lobster New Orleans Salmon; Big Marty's Sesame Hamburger Bun; Wonder Hot Dog Bun; Starbucks Venti Iced White Chocolate Mocha; Rigatoni Bolognese; Kroger Brown Sugar Hickory Baked Beans (logged typo); Stouffer's Chicken Alla Vodka; and Farmhouse Large Eggs. The locally retained audit row preserves the exact logged names/brands and outcomes. None reached the serving-match stage because identity failed first. The list includes generic, restaurant, and typo-bearing identities precisely to avoid silently accepting a merely similar USDA record.

## Customization and goal-card behavior

`home_cards.py` is the shared, UI-independent card and preference layer. Defaults are Protein, Fiber, Total Sugar. The Streamlit view supports pin, unpin, reorder up/down, hide, and restore, with any number of pins. It stores only stable IDs in owner-only `dashboard_preferences.json` in the local nutrition data directory, then republishes the static defaults. Corrupt/oversized preference data falls back safely; retired IDs are ignored and new metrics remain visible but unpinned. No preference is written to raw Lose It tables.

The static HTML contains the same card model plus device-local controls backed by `localStorage` key `nutritionHomePreferencesV1` (only pin/hidden metric IDs), with an in-memory fallback if storage is unavailable. A user’s device-local choices take precedence over the published Mac defaults on that device. Period and Nutrients/Food Patterns view selections also persist locally. Hidden metrics leave the new nutrient table and attention section; pinned metrics appear once in the pinned area. No passwords, token, diary data, or food research is in preference storage.

Minimum cards show capped adequacy progress toward the daily-average target. Maximum cards use a visually separate limit bar and describe the amount **over** a limit; they never call excess “positive progress.” Protein displays 150 g/day and the 130 g/day On Track floor. Saturated Fat shows `<10% kcal`; Sodium shows `≤2,300 mg/day`. Informational Total Sugar, Total Fat, Carbohydrates, Cholesterol, and Calories without a validated Lose It allowance have no goal bar. Weight has no completion bar and says “unit unconfirmed” when appropriate. A future scored range can show position within a range without treating the upper bound as a target.

“Needs Attention” temporarily shows up to two **unpinned, unhidden**, reportable metrics with Needs Attention or Significantly Off Track after at least two eligible days. Incomplete or informational metrics cannot enter this section. It does not alter permanent pin order. The static and Streamlit views share the same rule; the static client recomputes after local customization.

### Real-data card validation, 2026-09-26

Values below use the live metric engine, not hard-coded UI numbers. Current Week has five eligible logged days, Last Week seven, and Rolling 30 Days twenty-nine; included days without explicit completion are provisional. Goal comparisons use the period’s daily mean, so future or unlogged days are not counted as zero.

| Card | Current Week | Last Week | Rolling 30 Days | Presentation checked |
|---|---:|---:|---:|---|
| Protein | 93.0 g/day | 117.3 g/day | 108.9 g/day | 62%, 78%, 73% toward 150 g/day; capped adequacy bar; 130 g/day floor visible |
| Fiber | 21.5 g/day | 21.9 g/day | 23.0 g/day | 77%, 78%, 82% toward 28 g/day |
| **Total Sugar** | 108.5 g/day | 83.5 g/day | 91.0 g/day | Informational, no added-sugar limit or progress bar |
| Saturated Fat | 10.7% kcal | 14.8% kcal | 13.7% kcal | `<10% kcal` goal; 0.7, 4.8, 3.7 percentage points over limit; limit bar |
| Sodium | 2,706.8 mg/day | 3,412.2 mg/day | 3,226.5 mg/day | `≤2,300 mg/day` goal; 406.8, 1,112.2, 926.5 mg/day over limit; limit bar |
| Calories | 1,917.0 kcal/day | 2,253.6 kcal/day | 2,187.9 kcal/day | Informational intake; no invented Lose It allowance |
| Weight | 196.4, unit unconfirmed | 197.8, unit unconfirmed | 196.4, unit unconfirmed | Context only, no target or conventional progress bar |
| Added Sugar | Unavailable | Unavailable | Unavailable | Incomplete, not promoted |

With the default three pins, Current Week surfaces **Sodium** once in Needs Attention; Last Week and Rolling 30 Days surface **Saturated Fat and Sodium** once each. Test cases additionally exercise five pins, moving a pin, hiding/restoring a pinned metric, corrupt preference fallback, and exclusion of pinned/hidden/incomplete/informational attention candidates.

## Weekly job, publication, and safety

The existing LaunchAgent `com.local.loseit-readonly.weekly-pattern-audit` remains loaded for **Sunday 11:30 AM local time**, after the daily 10:00 AM update. Its Python command now uses the restricted USDA client when the owner-only key is configured, otherwise local-only fallback. Candidate count and source calories are local ranking inputs. The controlled manual live run, provider-error retry, and immediate repeat created no new nutrition evidence and no duplicate final audit row; the last run made zero USDA requests. Failure is contained to the weekly job and does not alter the daily update. No Harbor cloud migration was made because this workflow needs the local SQLite repository and iCloud destination.

The updated iCloud HTML/JSON expose three periods, Nutrients and Food Patterns, semantic goal cards, the compact attention section, and safe metric-ID preferences. Published files were scanned for credentials, headers, raw snapshots, and internal USDA audit fields; no forbidden marker was present. The emitted JavaScript passed a syntax check. Browser URL policy blocked opening the local/iCloud HTML in the available browser tool and expressly prohibited alternate routes, so **375px phone, 768px tablet, and desktop visual layout and interaction screenshots could not be verified**. Static markup/CSS inspection, semantic model tests, script syntax, and exported-file assertions passed; visual/browser behavior remains a verification limit.

Final static publication was `2026-09-27T02:59:54Z` (September 26 in New York). HTML SHA-256: `beebca9aa89f7d22840c4207035de99dd6b92efdd978adb2a3c19a8f49486019`. JSON SHA-256: `a7a738c458de8a987d00795395f44a45e19067b732dde695ff6ea595734ee860`. Both files were read back and scanned after publication.

The live database still has 283 raw diary snapshots, 17 raw weight snapshots, 1,270 normalized occurrences, 265 canonical foods, and 280 formulations. Row digests for all five of those tables exactly match the pre-pass backup. SQLite integrity is `ok`. The exact Lose It MCP read-only allowlist was not modified; no workout app code was touched. iPhone deployment: **N/A**. iPad deployment: **N/A**. Apple Watch deployment: **N/A**.

## Numbered follow-up implementation record

Each entry includes disposition, files, live result, verification, and remaining limit/follow-up. “No new evidence” below reflects the conservative USDA outcome, not a failed request boundary.

1. **USDA approval — applied.** Files: `restricted_usda.py`, `food_patterns.py`. The provider receives name/brand only; thirty live identities were queried. Exact-field HTTP tests pass. Scope remains USDA only.
2. **Request privacy audit — complete.** Files: `restricted_usda.py`, `tests/test_home_cards_restricted_usda.py`. Local counts never enter the client; fake transport proves only the approved search body and FDC-ID detail path. Future providers require their own review.
3. **Restricted research — complete, no accepted match.** Files: `food_patterns.py`, `weekly_scheduler.py`. Thirty identity queries were logged locally; four pre-existing Added Sugar foods and 22.3%/10.4% rolling coverage remain. Exact-match rejection and retry behavior were checked. More precise product identifiers/servings would be needed for additional evidence.
4. **Customizable Home — delivered.** Files: `home_cards.py`, `dashboard.py`, `publication.py`. Local pin/unpin/reorder/hide/restore works with >3 pins; tests cover all actions. Static browser state is per device rather than shared back to the Mac.
5. **Goals on cards — delivered.** Files: `home_cards.py`, both renderers. Protein, Fiber, Sodium, and Saturated Fat show current value, target, and semantic progress. Live labels above and static assertions verify them. Unavailable Added Sugar stays unscored.
6. **Semantic bars — delivered.** Files: `home_cards.py`, `dashboard.py`, `publication.py`. Adequacy caps at 100%; over-limit uses explicit overage text and distinct color; informational/trend omit bars. Unit tests cover each type. Product bands remain reference UX, not clinical guidance.
7. **Period-aware goals — delivered.** Files: existing `metric_engine.py`, `home_cards.py`. Three periods use eligible daily means; 5/7/29 live eligible days. Existing period tests and live comparisons pass. Completion-unknown days remain provisional.
8. **Needs Attention — delivered.** Files: `home_cards.py`, both renderers. Current Week surfaces Sodium; other periods surface Sodium and Saturated Fat without duplicating pins. Tests exclude incomplete/informational/hidden metrics. Limited to two cards.
9. **Card hierarchy — delivered in code.** Files: both renderers. Current amount has the largest type, with goal/progress/status/context below. HTML/CSS inspected and script syntax checked. Pixel-level device inspection was blocked by browser policy.
10. **Two views — preserved.** Files: `dashboard.py`, `publication.py`. Shared period state remains across Nutrients and Food Patterns; static has view buttons. Tests verify both view structures. Browser interaction could not be visually exercised.
11. **Weekly audit — restricted USDA enabled.** Files: `weekly_scheduler.py`, `food_patterns.py`. Loaded Sunday 11:30 AM job, live controlled/repeat runs, no duplicate evidence. A missing key safely leaves local-only mode. Exact identity scarcity limits enrichment.
12. **Preference storage — delivered.** Files: `home_cards.py`, `publication.py`. Owner-only JSON and browser-local metric IDs survive refresh; corrupt data falls back; new/retired IDs handled. Tests verify file mode and normalization. Device choices are intentionally not synced back.
13. **Static iCloud — delivered.** Files: `publication.py`. New goals, bars, selector, view toggle, preferences and attention are in HTML/JSON; privacy and script checks pass. Browser policy prevented rendered screen checks.
14. **Real-data UI validation — code/data complete; rendered check blocked.** Files: this report, tests. Live card values and copy are above. The browser tool rejected the local HTML URL and prohibited alternate routes, so width-specific screenshots are outstanding.
15. **Backend continuity — preserved.** Files: shared card layer and limited weekly additions only. Existing period/status/food-pattern engines remain intact. Full regression suite passes. No foundational redesign required.
16. **Raw data/security — verified.** Files: privacy tests and this report. Five source/resolved tables retain exact row digests; SQLite integrity `ok`; no Lose It write capability. Future source changes require their own baseline.
17. **Testing — complete.** Files: new privacy/card tests and static export assertions. Full suite, Ruff, syntax check, and privacy scan passed. Rendered browser QA remains blocked.
18. **Documentation — updated.** Files: governing spec and this dated report. USDA boundary, unchanged coverage, preference model, card semantics, weekly behavior and limits are recorded. Revise when evidence or data changes.
19. **Reporting — complete.** Files: this dated section. It includes egress, research outcomes, before/after, live cards, customization, weekly job and limitations. Counts are point-in-time.
20. **Regression audit — complete.** Files: `restricted_usda.py`, `home_cards.py`, tests. USDA rejected a punctuated approved product query with HTTP 400; normalizing only name/brand words fixed it and a regression test locks that behavior. Code review found that an exact USDA category alone could mark a mixed dish such as tomato sauce as meaningful vegetables; the restricted client now excludes mixed-dish identities and has a test. Weight initially displayed the literal “source unit”; it now says “unit unconfirmed.” The first customization-control iteration contained an empty action option; it was corrected before release. No unresolved code regression is known; visual browser QA remains unverified.
21. **Git — to be recorded at pass completion.** Files: this implementation set only. Commit/push/hash and final tree state appear in the final task response. Pre-existing `.DS_Store` and `missing_coverage.json` remain untracked.
22. **Device deployment — N/A.** No workout app files changed. iPhone: N/A; iPad: N/A; Apple Watch: N/A. Future workout integration remains separate.
