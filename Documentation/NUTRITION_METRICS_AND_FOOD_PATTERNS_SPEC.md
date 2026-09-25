# Nutrition metrics and food patterns: research and implementation specification

Research/design pass, 2026-09-25. **No feature, migration, scheduled job, or production-data change is authorized by this document.** All dates below use `America/New_York` calendar dates. This is a product specification, not individualized medical advice.

## 1. Executive summary and evidence labels

**Recommendation.** Build two tabs, **Nutrients** and **Food Patterns**, with a shared persistent period selector. Start with Protein and Fiber as prominent cards. Make Added Sugar a *coverage-gated candidate*, not a scored primary card now. Keep Calories and Weight accessible as secondary context. Use separate nutrient direction types, explicit unknown states, and a versioned classification extension to the Resolved Food Library. Daily rendering must be deterministic and independent of weekly research.

Throughout this document, **[E]** means an established recommendation or finding from a cited source; **[I]** means a reasonable design inference from evidence and repository data; **[P]** means a proposed product choice to approve before implementation. Status bands are [P] UX bands, **not clinical cutoffs**. The citations in §19 include publication/source context and exact links.

## 2. Repository and live-data audit

Repository: `loseit-readonly`, branch `feat/read-only-nutrition-repository`, schema version 8. Read-only audit of `/Users/kylevancleave/.local/share/loseit-readonly/nutrition.sqlite3` on 2026-09-25, opened with SQLite `mode=ro&immutable=1`; `PRAGMA integrity_check` returned `ok`. `immutable=1` was necessary in this sandbox because normal `mode=ro` could not open the live path; this is a read-only on-disk snapshot and may omit uncheckpointed WAL writes if any were present. Current state:

| Object | Count / range |
|---|---:|
| Raw diary snapshots | 280, dates 2026-01-01–2026-09-25, 268 distinct dates |
| Raw weight snapshots | 16 |
| Current normalized occurrences | 1,262, dates 2026-04-17–2026-09-24, 161 logged dates |
| Distinct current source food IDs / source identity rows | 266 / 266 |
| Canonical foods / formulations / active formulations | 265 / 280 / 265 |
| Formulation nutrient rows / occurrence resolution history rows | 2,520 / 1,295 |
| Audit runs / anomaly findings / invalid source fields | 7 / 26 / 21 |
| Weight observations | 105, dates 2026-01-05–2026-09-24 |

The old `enrichment_queue` has 266 rows marked `unresolved`; it is **not** the current resolved-library research backlog. The derived research queue is **0** (`266 complete`), with **0** uncleared source-review flags and **0** actionable Needs Your Help questions (the latest proposal actions are one `approved`, three `resolved_library`). The Resolved Food Library covers the 266 source IDs with 265 canonical foods, and no `food_patterns`/classification table exists. The earlier foundation report's counts are stale. The 2026-09-25 diary snapshot exists but has zero entries; the latest actual food log is 2026-09-24. Recent `day_completion_observations` say `unknown`, so a logged day cannot be asserted to be finalized. [I]

The nine standard nutrients are calories, protein, carbohydrate, total fat, saturated fat, fiber, total sugar, sodium, and cholesterol (`repository.py::STANDARD_NUTRIENTS`). `resolved.py::combined_values_for_occurrence` chooses the defensible source or formulation value and retains provenance. In the four tested periods below, every occurrence had a combined value for each of the nine; coverage does **not** mean all source values are exact. For the trailing 30 days, protein was source-derived on 227/234 occurrences, fiber on 215/234, total sugar on 209/234, saturated fat on 216/234, and sodium on 221/234; remaining values came from researched/representative/modelled/user-confirmed formulation records. [I]

The stored diary payload consists of date, entries, daily nutrient totals/coverage, and total calories. Entry nutrients include total `sugar_g` and some `unknown_nutrient_*` ordinals, but no labeled added-sugar quantity. The unknown ordinals must remain unknown: their meaning is not validated. There is no stored daily Lose It calorie allowance, exercise adjustment, or daily budget field. `weight_observations.unit` is null in recent rows even though magnitudes and user context indicate pounds; the next pass must confirm the source unit rather than assume it. The current local daily automation is a macOS LaunchAgent in `scheduler.py`, not Harbor. It runs a normal update at local 10:00; a separate weekly local job is the cleanest later addition. [I]

## 3. Period, denominator, and comparison contract

| Selector | Exact dates | Status denominator | Comparison |
|---|---|---|---|
| Current Week | Local Monday through today, inclusive | Completed/eligible logged days through today; never seven until Sunday | Same elapsed weekday span of prior week, plus optional full Last Week context |
| Last Week | Previous Monday–Sunday | Seven dates only when all seven are eligible; otherwise show available-day count and incomplete label | Week before Last Week |
| Rolling 30 Days | Today and preceding 29 calendar dates | 30 dates for coverage, eligible logged days for observed mean | Immediately preceding nonoverlapping 30 dates |

**Eligibility [P].** A date with explicit source completion is eligible. In the current repository, all recent completion states are `unknown`; for an initial read-only example, past dates with at least one occurrence are *provisionally eligible*, and today is excluded from unfavorable minimum judgments until explicitly completed. A retrieved empty day is not a zero-intake day without explicit completion. The UI must show `n eligible / N calendar days`, freshness, and a provisional marker. If an entire period lacks eligible days, report `Insufficient data` rather than a status. For a maximum metric, a partial current day may trigger a provisional warning once the *observed* amount already exceeds its full-day limit, but a low partial intake earns no positive status.

**Aggregation [P].** For each nutrient, sum occurrence values by date using the existing combined-value precedence; never sum source and replacement together. Period `daily average = sum(eligible-day values) / eligible-day count`. Pace for a daily minimum/maximum through Wednesday = `daily threshold × 3 eligible calendar days`, with gaps called out. Rolling 30 is a *true* 30-date window, not a month; compare daily averages to avoid misleading day-count differences. Show totals only on drilldown. Do not double count a food's categories in a single category metric. Use exact, unrounded values for thresholds; round only display. Do not compare a five-day Current Week directly with a full seven-day prior week. A change arrow indicates numerical movement and should say whether that is beneficial only when direction is meaningful and quality is sufficient.

**Coverage gate [P].** Any quantitative status requires all eligible-day occurrences to have a usable value *or* at least 95% occurrence and 95% source-calorie-weighted coverage with the missing fraction displayed; use `Insufficient data` below that. For direction-only food patterns, report classified occurrence/calorie coverage separately and do not interpret absence of classification as absence of consumption. The 95% cutoff is a product safeguard, not a scientific threshold. Revised/enriched classifications should trigger recomputation without changing raw snapshots.

## 4. Nutrient metric registry

`type` is explicit. All mean/status rules use §3 and §5. `CW/LW/30` below means Current Week / Last Week / Rolling 30 Days. A Lose It activity adjustment changes **only calorie budget**; other nutrient guidance does not automatically increase after exercise. Saturated-fat percentage uses **actual intake calories**; when intake calories are missing, show grams without a percentage status. For food-pattern serving advice, 2,000 kcal examples are references rather than personalized prescriptions. [I/P]

| Metric | Type; proposed benchmark | Why, windows, presentation | Weight/activity and prominence; limitation |
|---|---|---|---|
| Protein | **Target with adequacy floor**: 150 g/day central, 130 g/day practical floor | Muscle retention/gain during training and modest deficit; CW paced sum and mean, LW/30 mean plus `% of 150`, prior mean [E/I, §6] | Revisit at major weight/body-composition change, not every weigh-in; no exercise calorie scaling. **Primary**. |
| Fiber | **Minimum**: 28 g/day reference (FDA DV); optionally contextualize as ~14 g/1,000 kcal | Diet quality and gastrointestinal/cardiometabolic health; CW pace, LW/30 mean and `% of 28` [E/I, R3] | Do not automatically lower it on low-calorie days or raise on exercise days. **Primary**. |
| Added sugar | **Maximum candidate**: FDA 50 g/day DV at 2,000 kcal as provisional reference; current DGA advises ≤10 g per meal | Distinguishes added from intrinsic sugar; CW/LW/30 mean only when coverage gate met [E/I, R2/R3] | **Unavailable for scoring now**; do not use total sugar as proxy. Future source-specific goal may be chosen; no activity bonus. Candidate primary only after coverage proof. |
| Total sugar | **Trend/informational**; no universal limit | Explains sweet-food pattern but includes fruit/milk sugars; CW/LW/30 mean and contributor mix, no percent/status [E/I, R2] | Secondary; cannot support added-sugar inference. |
| Saturated fat | **Maximum**: <10% of actual consumed kcal, not fixed grams | Cardiovascular pattern; CW/LW/30 = `9 × sum(sat-fat g) / sum(actual kcal)` ×100, with grams/day secondary [E, R2] | No exercise bonus; actual kcal denominator can shift during deficit. Secondary, alert on sustained excess. |
| Sodium | **Maximum reference**: <2,300 mg/day general adult guideline | Useful with processed/restaurant food; CW paced, LW/30 mean, limit ratio [E, R2] | Heavy sweat/medical exceptions require individual advice; no automatic activity adjustment. Secondary. |
| Total fat | **Target range context**, 20–35% of actual kcal adult AMDR, but **unscored by default** | Macro balance without penalizing a higher-protein deficit; CW/LW/30 energy share and grams/day [E/I, R4] | Do not replace personalized plan or reward lower fat. Secondary. |
| Carbohydrate | **Target range context**, 45–65% actual kcal adult AMDR, **unscored by default** | Training fuel/macro context; CW/LW/30 energy share and grams/day [E/I, R4] | Resistance training/deficit preferences may justify different shares; no hard target. Secondary. |
| Cholesterol | **Trend/informational**, FDA 300 mg label DV is context, not a personalized ceiling | Keep visible for transparency; CW/LW/30 mean and sources [E/I, R3/R5] | AHA dietary-pattern advice does not support a universal standalone status. Secondary. |
| Calories | **Dynamic daily budget/target**: each day's actual Lose It adjusted allowance | CW/LW/30 `sum(actual kcal) − sum(matched daily allowances)` plus daily mean deviation, only for days with both values [P] | Budget is **not stored now**, so no adherence/status until a validated read-only acquisition path exists. Secondary. Never substitute a fixed target. |
| Weight trend | **Trend/informational**; user-desired ~0.5–1.0 lb/week now, ~0.5 lb/week around 190 lb as context | CW latest 7-calendar-day mean, LW comparable 7-day mean, 30-day trend slope and observation counts; no completion percentage [P] | User may stay above 180 lb based on body composition. Secondary; unit missing in DB and sparse weigh-ins limit precision. |

### Calories detail

Store a versioned `daily_allowance_observation(date, base_goal, activity_adjustment, adjusted_allowance, retrieved_at, source_snapshot_ref, provenance)` only after verifying a permitted **read-only** Lose It response supplies it. Never calculate an allowance by reverse-engineering weight, apparent deficit, or calories consumed. For matched eligible days, compare `Σ intake` against `Σ adjusted_allowance`; missing allowance days are excluded and named. A proposed descriptive band is within ±5% of the sum = On Track, 5–10% over/under = Slightly Off Track, >10–20% = Needs Attention, >20% = Significantly Off Track; **do not label under-budget as Excellent**, because larger deficits may impair training/recovery. The app might need day-level budget finalization because exercise credits can arrive after eating. This is product logic pending real allowance data and user approval, not a dietary prescription. [P]

## 5. Graduated status engine, exact proposed logic

Evaluate quality gate first, then the metric-specific rule. No green/excellent status for a partial day merely because a maximum has not yet been crossed. No arbitrary status for informational metrics. The exact bands below are [P] display heuristics, not evidence-derived biological boundaries.

| Class / metric | Excellent | On Track | Slightly Off Track | Needs Attention | Significantly Off Track |
|---|---|---|---|---|---|
| Protein daily mean | ≥150 g | 130–<150 | 115–<130 | 95–<115 | <95 |
| Fiber, ratio to 28 g | ≥1.00 | 0.90–<1.00 | 0.75–<0.90 | 0.50–<0.75 | <0.50 |
| Maximum: sodium/added sugar, ratio to limit | ≤0.80 | >0.80–1.00 | >1.00–1.10 | >1.10–1.25 | >1.25 |
| Saturated fat, ratio of energy share to 10% | ≤0.80 | >0.80–1.00 | >1.00–1.10 | >1.10–1.25 | >1.25 |

For protein, ≥150 g is adequate and **capped**: 180 g earns no additional score. Higher intake is not automatically better, and status is not a meal-by-meal demand. For minimum/maximum weekly display, calculate the period mean (or saturated-fat pooled share) then apply the band; also display day distribution so one high day does not disappear in an average. Food-pattern metrics use a separate directional vocabulary (`Evidence of pattern`, `Room to increase`, `Unknown`) until classification coverage is validated. The five graded labels are reserved for defensible quantitative metrics.

## 6. Protein recommendation

196–197 lb ≈89 kg, 180 lb ≈82 kg, and a consumer lean-mass estimate near 149 lb ≈68 kg. Thus 150 g/day is ~1.69 g/kg current weight, ~1.83 g/kg goal weight, and ~2.21 g/kg *estimated* lean mass. [I] The 2025–2030 US general guidance gives 1.2–1.6 g/kg/day [E, R2]. Resistance-training evidence finds a plateau around ~1.6 g/kg/day on average but with substantial uncertainty, and the key meta-analysis largely concerns energy balance [E, R6]. Energy-restricted athlete reviews support higher intake, especially in leaner athletes and more severe deficits, but their 2.3–3.1 g/kg fat-free-mass range is drawn from lean, trained populations and should not be applied rigidly to this user's imprecise consumer lean-mass estimate [E/I, R7].

**Proposal [P]:** use 150 g/day as a stable, simple *central adequacy target* for this modest deficit, with 130–170 g/day as an understandable working range. Treat 130 g as the practical floor for an On Track day/average and ≥150 g as Excellent/adequate, with no extra credit above 150. The 170 g upper end is not a safety ceiling; it marks where chasing more protein has little product value relative to fiber, produce, and overall diet. Reassess after a sustained weight/deficit/training change (especially near 190 lb), not automatically after every weight entry. The current observed 111 g/day over the recent logged 30 days is below this proposal, so the metric has practical value. Kidney disease or clinician-directed protein limits would require a different target; none is assumed here.

## 7. Added-sugar feasibility and rollout gate

**Current direct coverage: 0%** of 265 canonical foods, 1,262 current occurrences, and historical calories. No stored `added_sugar_g` row appears in `nutrient_observations`, `estimated_nutrients`, or `formulation_nutrients`; no labeled field appears in all 280 stored diary payloads. The Lose It projection in `service.py` maps total sugar ordinal 12, and raw unknown ordinals are unvalidated. There is one legacy enrichment version, no USDA-linked active formulations, and the current USDA provider's `NUTRIENT_NAMES` map excludes added sugar. These are repository findings, not proof that Lose It's service can never supply it. [I]

**External feasibility [E/I].** US Nutrition Facts labels require added sugars on packaged foods [R3]. USDA FoodData Central Branded data may carry manufacturer label values and ingredient/serving information, but coverage, identity, serving equivalence, and historical formulation must be checked per product [R8]. Manufacturer archived/current labels can supply exact product values; restaurant menu nutrition may or may not. A generic food naturally containing no added sweetener can receive a *verified zero* only when food identity and preparation exclude added sugar. Plain milk, fresh/frozen plain broccoli, plain eggs, plain white rice, and plain butter are examples of a **candidate** zero group; specific branded/flavored variants still require ingredient checks. An ingredient list with sugar but no amount is **presence evidence, not grams**. Total sugar is never copied into added sugar.

**Measured lower bound from a conservative pilot [I]:** six distinct recent plain-food identities matching those examples account for 69/234 (29.5%) trailing-30-day occurrences and 8,875/63,262 (14.0%) source kcal; across history they account for 276/1,262 (21.9%) current occurrences and 33,952/350,954 (9.7%) source kcal. These are *potential verified-zero candidates*, not already researched measurements. Thus today's defensible stored numerical coverage remains **zero**. 245/265 canonical foods have a nonempty brand string; 61/67 trailing-30-day identities are branded, representing 168/234 occurrences and 54,070/63,262 kcal. That is an **addressable-label pool**, not an exact-match success rate. A preliminary working hypothesis is that a focused label pilot might reach roughly 40–70% of canonical foods and 50–80% of historical occurrence/calorie weight; this is **low-confidence feasibility planning**, not a measured result. The next pass must sample at least 30 high-impact identities across packaged, restaurant, generic, and historical foods and replace these ranges with audited denominators before setting rollout dates.

**Rules [P].** Store added sugar separately with source class (`source_labeled`, `current_product_label`, `historical_product_label`, `USDA_exact_product`, `generic_verified_zero`, `representative_estimate`, `unknown`), serving/identity evidence, research date, evidence URL, confidence, and formulation version. Current product labels cannot silently backfill prior formulations. `known zero` differs from `unknown`. Do not derive added sugar as total sugar minus an assumed natural fraction. Estimates may be shown as ranges only with explicit basis, never in an exact grams total. Exclude the metric from graded status until ≥90% occurrence **and** ≥90% calorie coverage in the selected period, with no dominant uncovered high-sugar product; show the coverage denominator. [P] Until then, use Fiber as the second primary card and a **Sweet foods / added-sugar sources (directional)** food-pattern note if classification is defensible. Total sugar remains a secondary descriptive metric, not the fallback limit.

## 8. Validation on actual recent data

Values below were recomputed read-only with `resolved.combined_values_for_occurrence`, 2026-09-25 local date. Averages divide by days with logged food, not by empty/unconfirmed dates. Current Week is **provisional** because completion flags are `unknown`; September 25 has an empty snapshot. Saturated fat uses pooled actual calories. All nine standard fields have 100% combined occurrence coverage in these windows, but some values are representative estimates. `n/a` means no trustworthy stored budget, not adherence failure. [I]

| Metric / proposed status | Current Week 9/21–9/25: 4 logged days, 29 entries | Last Week 9/14–9/20: 7 logged days, 58 entries | Rolling 30 8/27–9/25: 29 logged days, 234 entries |
|---|---:|---:|---:|
| Protein | 98.2 g/day; **Needs Attention**; 65% of 150 | 117.3 g/day; **Slightly Off Track**; 78% | 111.0 g/day; **Needs Attention**; 74% |
| Fiber | 21.1 g/day; **Slightly Off Track**; 75% of 28 | 21.9; **Slightly Off Track**; 78% | 22.8; **Slightly Off Track**; 81% |
| Sodium | 2,794 mg/day; **Needs Attention**; 121% of 2,300 | 3,412; **Significantly Off Track**; 148% | 3,227; **Significantly Off Track**; 140% |
| Saturated fat | 10.5% kcal; **Slightly Off Track** | 14.8%; **Significantly Off Track** | 13.6%; **Significantly Off Track** |
| Total sugar (informational) | 96.3 g/day | 83.5 g/day | 88.4 g/day |
| Calories (intake only) | 1,813 kcal/day; budget **n/a** | 2,254 kcal/day; budget **n/a** | 2,181 kcal/day; budget **n/a** |

Current Week protein pace through four logged dates is 393/600 g (65%); full-week 1,050 g is shown only as a full-week reference, **not** the denominator used for status. Current Week sodium is 11,174/9,200 mg (121% of four-day limit). The immediately preceding 30 days (7/28–8/26) had 30 logged days, 97.6 g/day protein, 20.1 g/day fiber, 3,189 mg/day sodium, and 2,038 kcal/day intake; these are descriptive comparisons, not causal conclusions. The latest seven-calendar-day weight window ending 9/24 has four observations averaging 196.62 in a database with null unit; the preceding seven days have five observations averaging 198.54. This apparent ~1.9 unit change is **low-confidence as a rate**, given sparse measurements and normal short-term water/sodium fluctuation. [I]

## 9. Food Patterns: metrics and temporal semantics

Food Patterns answers whether named dietary components appear regularly, without implying ingredient-perfect knowledge. [I/P] Prioritize explicit portion equivalents when reliably mapped; otherwise use a categorical, **directional** contribution. Every metric displays `verified quantity` and `additional directional evidence` separately. The pattern tab shares the selected period and exposes classification coverage. No black-box score.

| Pattern | Evidence-based reference and display | Current Week / Last Week / Rolling 30 rule |
|---|---|---|
| Vegetables | 2025–2030 DGA: ~3 servings/day at 2,000 kcal; AHA: ~2.5 cups/day at 2,000 kcal [R2/R9]. Show verified cup-equivalents/day where possible, plus days with meaningful vegetable intake. | CW count/pace through eligible days; LW daily mean and days; 30 daily mean, days, and subgroup mix. Do not imply a precise deficit from unknown mixed dishes. |
| Fruit | DGA: ~2 servings/day; AHA: ~2 cups/day [R2/R9]. Prefer whole fruit; 100% juice is separate/limited. | Same; separately report no observed fruit versus incomplete classification. |
| Seafood | AHA: two ~3 oz cooked fish servings/week, particularly fatty fish [R10]. Show verified cooked-oz or serving-equivalents plus qualifying occasions. | CW pace for ~2 occasions/week, LW count, 30 count and approximate weekly rate. Shrimp qualifies as seafood. |
| Fatty / omega-3-rich fish | Subset of seafood; salmon, sardines, herring, mackerel, trout with evidence; shrimp does **not** qualify solely because it is seafood [R11]. AHA emphasizes fatty fish; no separate universally mandated count. | Directional `≥1 meaningful fatty-fish occasion/week` is a **product prompt**, not an evidence-based separate quota; LW and 30 show occasions/4.29-week rate. |
| Whole grains | DGA: 2–4 servings/day scaled to energy needs; AHA: at least half of grains whole [R2/R9]. Show verified ounce-equivalents or grain-choice frequency; identify package “100% whole wheat” only after identity/ingredient confirmation. | CW/LW/30 mean verified equivalents and share of *classifiable grain occasions*; do not equate all oats-containing candy/bars with a serving. |
| Legumes | DGA and AHA favor beans, peas, lentils, soy [R2/R9]. No standalone universal daily quota for this user. | Show meaningful occasions/week and verified cup-equivalents if possible; a weekly ≥2-occurrence prompt is [P] habit guidance, not a guideline. |
| Nuts / seeds | DGA and AHA favor them within dietary pattern [R2/R9]; energy-dense portions matter. | Show meaningful occasions/week and verified ounce-equivalents; sesame seeds on a bun are trace, not a qualifying occasion. |
| Plant variety | DGA advises variety; American Gut observational work compared >30 vs <10 plant types/week but does **not** establish a clinical 30-plant threshold [R2/R12]. | Count distinct *meaningfully consumed* plant identities in CW/LW; 30-day count and trailing-week median, with a `directional exploratory` label and **no Excellent status or 30 target**. |

Additional measure to consider later: **sugar-sweetened beverages frequency**, only when product identity is clear; DGA discourages them [R2]. Do not add a generic processed-food or “healthy eating” score. [P]

## 10. Food classification and mixed-dish rules

Use a controlled taxonomy of `vegetable` (dark-green, red/orange, starchy, other), `fruit`, `seafood`, `fatty_fish`, `whole_grain`, `legume`, `nut_seed`, and `plant_identity`. A food can contribute to multiple categories (salmon: seafood + fatty fish; beans: legume + meaningful plant; broccoli: vegetable + plant), but only once per category per consumption event. FNDDS/MyPlate equivalents are appropriate only after exact ingredient/portion mapping; an app's `serving` or `piece` is not automatically a cup or ounce equivalent. [I/P]

Contribution has two independent axes: **quantity** (`exact_equivalent`, `estimated_interval`, `unquantified`) and **presence** (`none_verified`, `trace`, `meaningful`, `substantial`, `unknown`). Quantitative totals contain exact equivalents only; estimated intervals are shown separately as ranges. [P] `meaningful` requires a standalone portion, a verified recipe component of at least ~¼ cup produce/legume, ~½ oz nuts/seeds, ~½ oz-equivalent whole grain, or ~1 oz cooked seafood; these operational cutoffs are **product heuristics** for inclusion, not nutrition guidelines. Amounts below them can be `trace` and do not count for occasions/variety. `substantial` can be used for ≥1 recognized serving with evidence. If an ingredient is known present but amount is unknown, classify `presence_known_quantity_unknown`, which can contribute to an “encountered” narrative but not the meaningful count.

Pizza tomato sauce, ketchup, garnish, spice, onion powder, sesame-on-bun, and incidental corn starch never become a full vegetable/plant serving. A restaurant entrée with named vegetables may be a directional `presence_known_quantity_unknown` entry; a photographed/menu-described substantial vegetable side can become `meaningful` with medium confidence, still not exact cups. Mixed dishes require component-specific evidence: recipe, manufacturer ingredients plus quantities, USDA FNDDS portion mapping, or user-confirmed preparation. For multiple entries of the same dish on one day, count servings if mapped; otherwise consolidate to a single occasion per meal. Repeated days are separate occasions. [P]

## 11. Read-only food-pattern practicality sample

The trailing 30 days contain 67 distinct source IDs, 234 occurrences, 63,262 source kcal. A deliberately small set of nine **name-obvious positive candidates** covers 23 occurrences / 5,322 source kcal (8.4%): plain frozen/fresh broccoli (11 occurrences), Red Lobster salmon (2), “100% Whole Wheat Bagel” (4), branded baked beans (2), Chipotle pinto beans (1), Chipotle fajita vegetables (1), Chipotle corn salsa (1), and blackened shrimp tacos (1). These have different evidence strength: salmon is seafood and fatty fish by identity, broccoli is vegetable, bagel needs an ingredient/portion check for precise whole-grain quantity, baked beans may have added sugar, and shrimp tacos have unknown seafood amount. This sample demonstrates why one generic serving multiplier would be misleading. [I]

No explicit whole fruit appears among the 67 recent food names; this is **no observed direct fruit**, not proof of zero fruit intake. Plain white rice, eggs, milk, butter, and plain broccoli are straightforward negative/zero-category candidates. Granola bars, protein powders, SlimFast drinks, cereals, restaurant entrées, fries, tacos, pasta, pizza-like foods, and homemade dishes need label/recipe review before ingredient-equivalent claims. [I]

**Automation estimate, not validated coverage:** exact-name rules could immediately assign *at least a narrow core* (roughly 10–20 of 67 recent identities, 15–40% of occurrences, depending on whether negative categories count); a further 20–35 identities appear researchable from package/menu/recipe evidence; some restaurant and homemade dishes will remain directional or unknown. Do not use these rough ranges as a public dashboard quality claim. The next pass must run a stratified, documented pilot over all 67 recent identities to measure automatic positive/negative classification, unresolved occurrence share, and calorie-weighted coverage before setting a quality badge. [I/P]

## 12. Proposed persistent schema and provenance

Extend the existing library via additive, append-only tables in the **next pass**. Proposed structures:

```text
food_pattern_taxonomy(id, key, parent_id, definition, taxonomy_version)
food_pattern_evidence(id, canonical_food_id, formulation_id nullable,
  pattern_key, plant_identity_id nullable, quantity_kind,
  equivalent_value nullable, equivalent_unit nullable, lower_bound nullable,
  upper_bound nullable, presence_class, provenance, confidence,
  evidence_basis, evidence_url nullable, source_date nullable,
  user_confirmed, valid_from nullable, valid_to nullable,
  supersedes_id nullable, content_sha256, created_at)
occurrence_pattern_resolution(id, occurrence_id, evidence_id,
  formulation_id, scale_factor nullable, mapping_method, confidence,
  content_sha256, created_at)
food_pattern_audit_run(id, scope_start, scope_end, cadence,
  status, model_or_research_version, summary_json, created_at)
```

Foreign keys tie evidence to the existing canonical food and optionally an exact formulation. Prefer formulation-scoped evidence for labels/recipes that can change. Historical occurrences resolve against appropriate version/date; absent historical fit remains unknown. Latest accepted evidence is selected by explicit precedence; do not silently overwrite or delete old assertions. Contradictory evidence creates an audit finding and review task, not automatic replacement. Add a user-confirmed override that is append-only, scoped to the exact food/formulation/portion or date range, with a reason; the user can later supersede it. Reuse `food_source_identities`, serving mappings, occurrence resolutions, and content hashes. No write capability to Lose It. [P]

Provenance classes: `source_explicit`, `manufacturer_exact_current`, `manufacturer_exact_historical`, `USDA_exact`, `USDA_generic`, `recipe_verified`, `strong_identity_match`, `representative_estimate`, `modelled_directional`, `user_confirmed`, `unknown`. Confidence: `very_high/high/medium/low`, independent of provenance. Exact quantity needs an identity and portion mapping; high-confidence **presence** does not imply high-confidence **amount**. `none_verified` requires evidence that the relevant component is absent; unknown stays null. [P]

Quality presentation [P]: four unobtrusive badges: **Verified** (mostly direct/exact/user-confirmed and high confidence), **Mostly reliable** (some strong matches), **Directional** (meaningful representative/category estimates), **Incomplete** (coverage below gate). A tap reveals source/estimate split, unknown share, and notable assumptions. Nutrient provenance comes from existing per-nutrient records; food-pattern provenance from the new evidence table. Never show a single undifferentiated “100%” when representative estimates fill gaps.

## 13. Daily update and weekly deep audit

**Daily deterministic stage [P].** After existing sync/resolution/audit, classify exact source IDs and stable formulations using accepted evidence. Apply transparent high-precision rules to new obvious foods only, recording rule version and evidence; route ambiguous names to an unresolved queue. Resolve occurrence portions, compute nutrient metrics and food-pattern metrics, and publish the static snapshot with freshness/quality badges. No AI or network research is required for rendering. The pipeline must be idempotent, bounded, and preserve raw snapshots.

**Weekly research stage [P].** Use a separate local weekly LaunchAgent/entry point rather than extending the daily critical path. The existing local scheduled architecture and repository lock can support a distinct weekly job with its own idempotent audit run, no duplicate research of unchanged source identity+formulation fingerprint, and a retry checkpoint. Rank new/ambiguous foods by occurrences and calorie exposure, review mixed dishes and obvious false positives, consult exact manufacturer/USDA/restaurant/recipe evidence where useful, record source URL/date/basis/limitations, and commit only defensible versioned classifications. Expensive or uncertain cases remain unknown/directional or become user questions. Audit changes against prior classifications and regenerate snapshot after accepted updates. Do **not** install it in this pass. Harbor is not currently part of this repository's execution path; introducing it would add deployment/auth/data-access complexity without clear benefit for a local private SQLite/iCloud workflow. [I/P]

## 14. Dashboard information architecture and customization readiness

Default Nutrients order: **Protein**, **Fiber**, then a small Added Sugar availability/coverage card only if a meaningful pilot exists; otherwise **Saturated Fat** as the third prominent quantitative card is preferable to Total Sugar. Secondary list: Sodium, Calories, Total Sugar, Total Fat, Carbohydrates, Cholesterol, Weight trend. [P] Calories remain easily accessible, but the user already sees them in Lose It. Food Patterns leads with Vegetables, Fruit, Seafood/Fatty fish, then Whole Grains, Legumes, Nuts/Seeds, and exploratory Plant Variety. Each card has a plain-language definition, period value, prior-period comparison, status or `Informational`, and quality/freshness affordance.

Persist one period selection across tabs in client state/local storage with a valid default of Current Week; URL query parameter may mirror it for sharing. Timezone comes from the nutrition profile, not the browser's travel timezone. At local midnight, refresh date bounds and freshness; if offline/stale, show last snapshot date. Future pin/hide/order configuration should reference stable `metric_key`, `semantic_type`, `default_order`, `eligible_for_attention_surfacing`, and `coverage_gate`, not array positions. Permit >3 pins. Attention surfacing should require persistent material deviation (e.g., two eligible days or prior completed period), known coverage, and never displace a user pin without explanation. **Do not implement customization in this pass.** [P]

## 15. Next Codex pass: implementation sequence and acceptance criteria

1. Add schema migration for versioned food-pattern evidence/resolution and optional added-sugar evidence. Preserve all source values and history; migration is additive with rollback-by-restore plan. Add explicit tests for append-only behavior, canonical identity/formulation changes, mixed-dish unknowns, and user override precedence.
2. Identify and validate a read-only source for **per-day adjusted Lose It allowance**. If none, ship Calories as intake-only with `Budget unavailable`; do not fabricate a goal. Validate weight unit at source.
3. Run a stratified 30+ identity added-sugar label pilot and full 67-ID recent food-pattern classification pilot. Record identity, serving, historical applicability, provenance, confidence, occurrence/calorie coverage; review gates before primary-card promotion.
4. Implement pure metric definitions/status functions keyed by semantic type. Test Monday/Sunday boundaries, current incomplete day, missing/empty day, DST/timezone, dynamic allowance sums, partial coverage, source invalidation, weight gaps, and prior-period comparisons.
5. Extend daily deterministic update and snapshot contract. Add weekly audit runner separately, with idempotency, bounded research, lock/retry, audit log, and no dependency from dashboard render to AI. Install a weekly job only in an explicitly authorized later deployment pass.
6. Implement Nutrients/Food Patterns tabs, shared period selector, quality detail, and secondary metrics. Defer pin/reorder/hide UI until its own approved scope. Verify static output contains no secrets.
7. Run existing unit/integration tests and lint; SQLite integrity and baseline/current data-count checks; end-to-end static snapshot comparison. Confirm MCP read-only allowlist, raw snapshot IDs/digests, and normalized occurrence IDs/counts are unchanged unless a separately approved ingestion explicitly updates them.

## 16. Open user/product decisions before implementation

1. **Protein UX:** approve 150 g central adequacy target, 130 g practical floor, and no extra credit above 150. Evidence supports the ballpark; exact status labels are [P].
2. **Third default card:** use Saturated Fat now, and promote Added Sugar only after audited ≥90% occurrence and calorie coverage. Added Sugar is currently unmeasured. [P]
3. **Food-pattern display:** approve separate verified quantities plus directional evidence; especially keep mixed-dish amounts unknown unless supported. [P]
4. **Threshold bands:** approve §5's graduated product bands and the provisional/incomplete-day behavior; these are UI judgments, not medical thresholds. [P]
5. **Weekly cadence:** approve a separate local weekly research job for a later installation pass; this specification alone does not install it. [P]

Do not ask the user to choose a fixed calorie goal; the dynamic Lose It budget is a stated requirement. Do not ask for a 30-plant clinical target; the evidence does not support one.

## 17. Source/data safety and verification notes

Read-only analysis used direct SQLite connections; no `NutritionRepository` mutation path, source API call, credential, token, or USDA key was used. `sources/` and all raw Lose It files remain untouched. Existing untracked `missing_coverage.json` predates this pass and is intentionally excluded from commits. Device deployment: iPhone N/A, iPad N/A, Apple Watch N/A. Workout app untouched.

## 18. Research limits

No actual USDA/manufacturer matching campaign was run; addressable-label percentages are **not** demonstrated coverage. The nutrition database is outside the Git repository and was inspected as a read-only immutable snapshot; concurrent post-audit updates could change live counts. Current day is not logged, completion is unknown, weight unit is null, and exercise-adjusted allowance is absent. No clinical history, sex-specific target, sweat-loss data, or exact body-composition measurement was provided. The proposal therefore avoids personalized sodium, cholesterol, calorie, and plant-variety prescriptions.

## 19. Primary sources

- **R1:** [USDA FoodData Central FAQ](https://fdc.nal.usda.gov/faq/) and [API guide](https://fdc.nal.usda.gov/api-guide/): database types, branded-label origin, API access.
- **R2:** [Dietary Guidelines for Americans 2025–2030](https://cdn.realfood.gov/DGA_508.pdf), pp. 2–6: protein, vegetables/fruit, whole grains, saturated fat, added sugar, sodium. Current policy guidance; serving references are not individualized targets.
- **R3:** [FDA Daily Values and required Nutrition Facts fields](https://www.fda.gov/food/nutrition-facts-label/daily-value-nutrition-and-supplement-facts-labels): fiber 28 g, added sugar 50 g, sodium 2,300 mg, saturated fat 20 g, cholesterol 300 mg label references.
- **R4:** [National Academies DRI discussion of AMDR](https://www.ncbi.nlm.nih.gov/books/NBK208887/): carbohydrate 45–65%, fat 20–35% adult energy ranges.
- **R5:** [AHA dietary cholesterol science advisory summary](https://professional.heart.org/en/science-news/dietary-cholesterol-and-cardiovascular-risk/top-things-to-know): emphasize dietary pattern, not a universal numerical cholesterol status.
- **R6:** [Morton et al., 2018, resistance-training protein meta-analysis](https://bjsm.bmj.com/content/52/6/376): mean plateau around 1.6 g/kg/day with uncertainty and limited energy-restriction applicability.
- **R7:** [Phillips & Van Loon, 2014, protein during athlete weight loss](https://pubmed.ncbi.nlm.nih.gov/25014731/) and [Helms et al., 2014, review in lean resistance-trained athletes](https://pubmed.ncbi.nlm.nih.gov/24092765/): higher intakes during deficit, with population caveats.
- **R8:** [USDA Branded Food Products Database documentation](https://fdc.nal.usda.gov/GBFPD_Documentation/) and [USDA branded data overview](https://fdc.nal.usda.gov/docs/USDA_Global_BFPD_1Pager_Apr2021.pdf): manufacturer label/ingredient sources; per-product presence must be verified.
- **R9:** [AHA food-group serving examples](https://www.heart.org/en/healthy-living/healthy-eating/eat-smart/nutrition-basics/suggested-servings-from-each-food-group): vegetables, fruit, grains, nuts/legumes, fish at a 2,000 kcal reference.
- **R10:** [AHA fish and omega-3 recommendation](https://www.heart.org/en/healthy-living/healthy-eating/eat-smart/fats/fish-and-omega-3-fatty-acids): two fish servings/week, particularly fatty fish.
- **R11:** [NIH ODS omega-3 fact sheet](https://ods.od.nih.gov/factsheets/Omega3FattyAcids-HealthProfessional/): species differences; salmon vs shellfish EPA/DHA patterns. Use classification, not invented exact EPA/DHA quantities.
- **R12:** [American Gut Consortium, 2018, mSystems](https://journals.asm.org/doi/10.1128/msystems.00031-18): observational comparison of self-reported plant diversity; not proof of a 30-plant clinical cutoff.
