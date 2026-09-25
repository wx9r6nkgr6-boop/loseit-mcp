# Lose It! read-only MCP and nutrition repository

A local-first, read-only integration for personal nutrition analysis. It is a
hardened fork of [`cabird/loseit-mcp`](https://github.com/cabird/loseit-mcp)
and uses the unofficial [`phitoduck/lose-it`](https://github.com/phitoduck/lose-it)
SDK for Lose It!'s private GWT-RPC interface.

> This is not an official Lose It! API or a FitNow product. The private web
> interface may change without warning. Review Lose It!'s current terms before
> use; this project makes no claim that private-interface automation is
> supported by Lose It!.

## What is read-only

The MCP exposes exactly seven tools:

- `search_food`
- `describe_food`
- `get_diary`
- `get_diary_range`
- `get_weight_history`
- `server_status`
- `whoami`

There is no `log_food`, `log_custom_food`, `log_weight`, `delete_entry`, update,
or edit tool. `build_server()` compares the real MCP registry to the constant
`READ_ONLY_TOOL_ALLOWLIST` and refuses to start if any unexpected tool is
registered. Tests independently pin the same discovery surface.

The MCP and CLI receive a `ReadOnlyLoseItService` facade. That facade has no
mutation methods. The inherited upstream `LoseItService` and the third-party
SDK still contain write implementations so future upstream rebases remain
manageable, but the facade keeps them out of both AI discovery and ordinary
manual commands. This is an application boundary, not an account-level scope:
Lose It!'s `liauth` session token itself is not read-scoped, because Lose It!
does not provide such a scope.

Food names and brands in compact search rows are untrusted user content. They
are flattened to one line and pipe characters are replaced, preserving the
upstream defense against forged table rows. Diary results use typed structured
output rather than delimiter-rendered rows.

## Architecture

```text
MCP client / read-only CLI
            |
            v
explicit seven-tool allowlist
            |
            v
ReadOnlyLoseItService facade
            |
            v
upstream loseit-mcp service -> phitoduck/lose-it -> private Lose It! GWT-RPC

loseit-sync -> exact raw JSON + normalized SQLite rows + enrichment queue
```

Normal synchronization uploads no diary contents to any third party: the core
sync contacts only Lose It and writes locally. There is no telemetry. An
explicitly configured USDA FoodData Central provider may receive normalized
food/brand search terms during the separate research stage; it never receives
diary records, Lose It credentials, dates, meals, or account metadata.

## Normal operation

After one-time setup, nothing is normally required. Open the local dashboard,
go to **Settings & Targets**, configure the iCloud snapshot folder, and click
**Install automatic updates**. The Mac then runs the same authoritative update
pipeline around 10:00 AM local time each day. It does not wake the Mac. A login
or wake after 10:00 triggers one catch-up attempt; the daily gate prevents
multiple redundant catch-ups.

The normal pipeline is:

```text
read-only Lose It sync → reconcile today + prior 7 dates → conservative research
→ safe auto-enrichment → Needs Your Help exceptions → analytics → iCloud snapshot
```

Optional: open the dashboard and click **Update My Nutrition Data** at any time.
If attention is needed, open **Review & Enrichment → Needs Your Help** and answer
the simple factual question or choose **I don't know**. On iPhone/iPad, open the
latest `nutrition_dashboard.html` from the configured iCloud Drive folder.

## Install

Python 3.12+ and [`uv`](https://docs.astral.sh/uv/) are recommended.

```bash
git clone <your-fork-url> loseit-readonly
cd loseit-readonly
uv sync
uv run pytest
```

## Authentication

The normal flow does not store a Lose It password:

1. Open the local dashboard.
2. Click **Update My Nutrition Data**.
3. If prompted, sign in to Lose It in Chrome or Brave and click
   **Reconnect Lose It**. Choose a browser profile when more than one exists.
4. The dashboard validates the imported browser session with one read-only
   request, stores it owner-only, and resumes the interrupted update.

The import uses the upstream SDK's supported Chrome/Brave cookie-store reader.
macOS may show a Keychain permission dialog. Profile listing is local-only and
does not decrypt cookies; cookie access happens only after the explicit reconnect
button. Tokens are never shown after import or returned in dashboard results.

Manual token paste remains a masked dashboard fallback. For developer/CLI use:

```bash
uv run loseit-mcp import-token
```

This writes `~/.config/loseit-readonly/liauth` with owner-only mode `0600`.
Verify without changing the account:

```bash
uv run loseit-mcp whoami
uv run loseit-mcp status
```

An alternate token file can be selected with `LOSEIT_TOKEN_FILE`. A token may
also be supplied as `LOSEIT_TOKEN`, but environment variables are more likely
to leak to child processes and diagnostics. `LOSEIT_USER_ID` is normally
unnecessary because the numeric ID is read from the JWT `sub` claim.

The inherited email/password login path remains available as a compatibility
fallback in the low-level service, but the read-only CLI does not accept a
password flag and the example configuration does not encourage password
storage. Tokens and session caches are never printed to normal logs. Permissive
POSIX token-file permissions are rejected.

`liauth` is a JWT bearer session with an expiry time and no refresh token. A
currently signed-in browser is therefore the password-free renewal source. If
neither Chrome nor Brave has a current Lose It session, sign in there first or
use the masked token fallback. The dashboard never asks for or stores a Lose It
password.

## Run the MCP

Stdio is the recommended local transport:

```bash
uv run loseit-mcp serve
```

Example MCP client entry:

```json
{
  "mcpServers": {
    "loseit-readonly": {
      "command": "uv",
      "args": ["run", "loseit-mcp", "serve"],
      "cwd": "/absolute/path/to/loseit-readonly"
    }
  }
}
```

`get_diary_range(start_date, end_date)` uses explicit `YYYY-MM-DD` dates,
includes both endpoints, and permits at most 31 calendar days. It reads each
day through the existing diary read. Each result includes all source-labelled
nutrients, per-nutrient totals, and coverage metadata. A partial daily total is
therefore distinguishable from a complete one. Entry IDs are labeled
informational because no update/delete tool exists.

## Manual sync

The default pulls the last seven days and matching weight history:

```bash
uv run loseit-sync --days 7
```

An explicit range is inclusive and limited to 31 days:

```bash
uv run loseit-sync --start 2026-08-01 --end 2026-08-31
```

Use `--no-weights` to omit weight history. A successful run prints a compact
JSON summary containing days, new raw snapshots, seen/new occurrences, reused
enrichments, queued foods, weights, database path, and unresolved-food count.

The default repository is
`~/.local/share/loseit-readonly/nutrition.sqlite3`. Override it with
`LOSEIT_DATA_DIR` or `--data-dir`. The runtime directory is ignored by Git.

## Storage model

SQLite is canonical and uses foreign keys, WAL journaling, schema migrations,
unique constraints, and date/food indexes. The surrounding layout is:

```text
data/
  nutrition.sqlite3
  raw/          immutable diary and weight response snapshots
  normalized/   optional portable exports
  enriched/     optional portable enrichment exports
  reference/    reviewed research input
  reports/      future weekly/monthly outputs
```

Core tables:

- `schema_version`, `sync_runs`: migration and run audit history.
- `raw_diary_snapshots`, `raw_weight_snapshots`: exact JSON plus retrieval
  timestamp, source range/date, SHA-256, and append-only file path.
- `food_occurrences`: stable normalized rows with raw/normalized names and
  brands, portion, meal, source IDs, source timestamp, and ingestion timestamp.
- `nutrient_observations`: one row per source nutrient with value, unit, and
  mandatory `loseit` provenance. Missing nutrients have no observation; they
  are never silently stored as zero.
- `food_references`, `food_aliases`, `enrichment_versions`,
  `estimated_nutrients`: cached research, confirmed aliases, append-only
  versions, URLs, match type, confidence, assumptions, review status, and
  optional uncertainty bounds.
- `occurrence_reference_links`: explicit record of every applied cache match.
- `enrichment_queue`: unresolved foods and non-binding fuzzy suggestions.
- `weight_observations`: idempotent date-keyed local weight history.

Source observations and estimates live in different tables. Importing an
estimate cannot overwrite raw JSON or a Lose It! nutrient observation.

### Resolved Food Library and integrity audits

The durable Resolved Food Library maps stable Lose It source IDs to canonical
foods and append-only formulations. Each formulation has explicit serving
mappings and nutrient-level provenance/confidence. Known foods reuse those
profiles for arbitrary quantities; gram conversion is used only when a stored
authoritative gram weight exists. A quantity change alone does not create a new
formulation, while a materially changed per-serving fingerprint is audited.

Normal updates also run a bounded daily plausibility audit, with weekly and
monthly gates recorded in `nutrition_audit_runs`. Audits preserve raw/source
values and append findings. A source nutrient can be marked invalid for the
combined analytics layer without being deleted or rewritten; the combined
layer then uses an explicitly provenanced formulation value. Provider failure
does not itself create a human question: representative or calorie-constrained
fallbacks are used when defensible, and only factual ambiguities that a person
can materially clarify remain in **Needs Your Help**.

## Enrichment workflow

Before researching foods, inspect the nutrition already present locally:

```bash
uv run loseit-sync --coverage
uv run loseit-sync --coverage --json
uv run loseit-sync --coverage --missing-only --json
```

Coverage opens the existing `nutrition.sqlite3` with SQLite `mode=ro` and
`query_only`, in a consistent read transaction. It does not create a repository,
run migrations, load authentication, sync, research, or contact any service.
`--data-dir` selects another existing repository. A missing database is an error.
Coverage cannot be combined with sync, queue, or enrichment-import options.

The target set includes the nine standard nutrients (calories, protein,
carbohydrates, total fat, saturated fat, fiber, sugar, sodium, cholesterol),
plus **every additional nutrient key stored in source or estimated observations**.
Keys and units are preserved; no nutrient is discarded because it is unfamiliar.
Fields stored as `unknown_nutrient_*` or with `source_unit` units retain those
labels; their identities and units are not inferred. These fields participate
in full-target coverage and priority. A separate `standard_source_complete`
flag and summary count identify foods complete for the nine named nutrients.
Each nutrient reports counts, occurrence percentages, and missing counts for:

- `source_reported`: finite Lose It! observations, including explicit zeros.
- `estimated_or_enriched`: existing occurrence links to the latest reference
  version, with a finite estimate or valid finite bounds. Bounds-only coverage
  and estimates that fill source gaps are reported separately. Older versions,
  unlinked references, and fuzzy suggestions do not count.
- `combined_usable`: the union of those occurrences, without double-counting.

This measures information availability, not nutritional adequacy or accuracy.
Linked estimates may be low-confidence or unreviewed; per-food reference status
shows confidence and review coverage. The current reference model does not
encode a validated portion conversion, so this report does not scale estimates,
compute nutrient totals, or imply that linked estimates equal consumed amounts.
Missing observations stay missing; absent values never become zeros.

Calorie-weighted coverage means the percentage of **source-reported calories**
belonging to occurrences with that nutrient available. It is emitted only when
every occurrence in the relevant group has finite, nonnegative source calories
and their sum is positive. Otherwise JSON contains `null` and an explanation;
terminal output shows `n/a`. Empty-database occurrence percentages are also `null`.

Per-food groups use source and stable source food ID; without an ID they use
source, normalized name, and brand. Distinct IDs are never merged just because names
match. The last stored occurrence supplies the display name when an ID has
name variants. Every target nutrient includes source/estimate/combined counts,
so partial coverage across repeated occurrences remains visible.

Foods sort by **occurrence frequency × average number of combined missing target
nutrients**, equivalent to the count of missing occurrence–nutrient slots.
This is a practical research-priority heuristic, not a scientific score. The
missing-nutrient count is the number of target fields missing on at least one
occurrence. `--missing-only` filters that per-food list; overall coverage totals
still describe the entire database. JSON includes full per-food measures and
definitions for a subsequent ChatGPT research step, without dates, meals,
account identifiers, entry identifiers, credentials, or raw diary snapshots.

**Unresolved enrichment status is independent of nutrition coverage.** It means
there is no high-confidence cached reference, not that source nutrition is
incomplete. A food can be unresolved and still have 100% source and combined
coverage. Coverage neither edits the queue nor creates estimates.

Synchronization never browses the web. Review the queue:

```bash
uv run loseit-sync --show-queue
```

Research deliberately, preferring exact manufacturer/restaurant sources,
USDA FoodData Central, other authoritative branded databases, consistent
generic references, and only then calorie-constrained inference. Copy
`examples/enrichment.json`, replace the placeholder URL and values, and import:

```bash
uv run loseit-sync --import-enrichment /private/path/reviewed-food.json
```

Every estimated nutrient must have a provenance value:

- `researched_exact_product`
- `researched_brand_equivalent`
- `researched_generic_food`
- `inferred_from_calories`

Each enrichment also requires a match type, confidence (`low`, `medium`, or
`high`), assumptions, research date, and source reference. Low-confidence
values can use `lower_bound` and `upper_bound`; do not manufacture precision.

An exact normalized food-name + brand match is reused automatically. Only
manually confirmed aliases are auto-applied. Fuzzy matches are suggestions in
the queue and never silently linked. Importing a newer reference creates a new
version rather than destroying prior provenance.

## Compact research queue: repeatable local workflow

```bash
# Normal sync is the only step here that contacts Lose It! (read-only).
uv run loseit-sync --days 7
uv run loseit-sync --coverage                       # optional audit
uv run loseit-sync --research-queue                 # concise terminal summary
uv run loseit-sync --research-queue --json > nutrition_research_queue.json
# Upload that JSON to ChatGPT for deliberate research outside this program.
# Review its product match, label basis, evidence and proposed values, then:
uv run loseit-sync --import-enrichment /private/path/reviewed-food.json
uv run loseit-sync --research-queue
```

This is an advanced deliberate workflow; normal scheduled updates use the
configured research provider and Needs Your Help inbox. The research queue
itself opens SQLite in read-only/query-only mode, does not load credentials,
contact any service, migrate the database, or write enrichment. The JSON export
contains food information: keep it private. Its default filename is gitignored.

Only these nine fields drive research: `calories`, `protein_g`, `carb_g`,
`total_fat_g`, `saturated_fat_g`, `fiber_g`, `sugar_g`, `sodium_mg`,
`cholesterol_mg`. Unknown fields remain unchanged in SQLite and `--coverage`,
but never enter this queue or its score. Missing is not zero; finite zero is
present. An unresolved general-cache item can have complete standard nutrition
and will then be omitted here.

JSON schema version 1 has `local_only`, `queue_count`, `standard_nutrients`,
`lifecycle_counts`, `rules`, and a sorted `foods` array. Each food includes:

- Stable `source_food_id` (or null), normalized `food_name`/`brand`, raw names,
  occurrence count, first/last seen dates, reference and general-cache status.
- Actual finite `source_nutrients` when identical across occurrences; otherwise
  null, with every distinct set in `portion_variants`. Variants retain logged
  amount/unit/servings, retained serving description, source values and count.
- `source_serving: null` and `source_basis_status`: current stored snapshots do
  not retain a reliable original label basis. Values are copied exactly, never
  divided by logged quantity or presented as an invented per-serving label.
- Source-missing and combined-missing standard fields, per-field missing
  occurrence counts, `priority_score`, matching status and `import_target`.

Priority is **occurrence count × distinct missing standard fields**, descending,
with deterministic ties. This is a practical heuristic, not a scientific score.
No product identity, UPC or external match is inferred from a brand string.

Lifecycle is derived each run: `needs_research`, `partially_filled`, `needs_review`,
or `complete` (counted but omitted from the worklist). Reuse requires the latest
linked version to be manually reviewed, high confidence, and an exact branded
or authoritative generic match, or a manually confirmed alias. An
`insufficient_information` match is never trusted. Low-confidence/unreviewed
references remain visible as needing review. Finite estimates or valid bounds
fill availability gaps, without overwriting source values. This queue deliberately
uses stricter reuse rules than the all-estimates audit in `--coverage`.
Availability does not certify portion-normalized nutrient totals.

### Reviewed import round-trip

Use the existing single-food `examples/enrichment.json` format: preserve its
required match type, confidence, source references, research date, assumptions,
manual review flag and per-nutrient provenance. Replace example values only
after research and human review. For a stable-ID queue item:

1. Copy its entire `import_target` object unchanged into the document's `target`
   field; keep its `food_name` and `brand`.
2. Add `nutrition_basis` with a positive numeric `amount`, explicit string `unit`,
   and nonempty `description` documenting the actually reviewed nutrition label
   basis. This is retained with the version, not used for guessed conversions.
3. Provide researched nutrient records in `nutrients`; never convert unknowns or
   missing values to zero. Set `manually_reviewed` to a JSON boolean.
4. Import one document per food, then regenerate the queue. Every new version
   replaces the prior version for reuse, so retain all still-valid reviewed
   nutrients, not just the newest additions. Older versions remain intact.

The target includes a source-context fingerprint. A stale export with changed
source values/portions is rejected before import writes; export again and review.
Repeated unchanged contexts do not invalidate it. Future occurrences of the
same ID and normalized identity reuse the reference, even at different logged
quantities; conflicting values at identical portions or changed identity require
review. A source-ID reference cannot silently spread through name-only matches
or aliases to another ID. The existing unique name/brand reference model also
rejects binding an already-bound reference to a second ID, or retargeting a
reference linked to other foods. Such collisions need explicit resolution, not
an automatic merge. No-ID foods retain conservative name/brand imports and
explicitly confirmed aliases; no stable target is fabricated.

Schema 2 adds append-only review context and source-target tables on a future
explicit writable sync/import. Both reporting modes still read schema 1 without
migrating it. No MCP tools or remote write capabilities are added.

## Historical backfill (2026 now; other years explicitly later)

```bash
uv run loseit-mcp compatibility-check
uv run loseit-mcp whoami
uv run loseit-mcp status
uv run loseit-sync --backfill-year 2026
uv run loseit-sync --backfill-year 2026 --json > nutrition_private_reports/backfill_2026.json
```

Create private output directories first if using redirection. Backfill processes
January 1 through December 31 of the requested year, capped at **local today**.
It never requests dates in another year. Older years use the same explicit
`--backfill-year YEAR` command; none are selected automatically. Future years
are rejected. There is no source API for a guaranteed latest available diary
date: empty responses through today are retained as retrieved-but-unlogged days.

Chunks contain at most 31 inclusive calendar days. They run sequentially through
the existing read-only diary-range and matching weight-history paths. Defaults
are **1 second between diary requests**, 1 second before weights, and **2 seconds
before each chunk**. `--request-delay` and `--chunk-delay` can tune these; each
must be finite and at least 0.25 seconds. Existing transport retries and weight
decoder subdivisions remain bounded. No uncontrolled retries, concurrency or
automatic GWT configuration refresh is added. Progress goes to stderr, so stdout
remains valid JSON.

Schema 3 stores `backfill_runs` and `backfill_chunks`. A chunk becomes complete
only after its complete date set and matching weights have been validated and
persisted. After failure, Ctrl-C, termination or restart, rerun the same command:
completed chunks in that unfinished pass are skipped. A process-level lock
prevents two backfills from operating on one repository simultaneously. Do not
run ordinary sync/import concurrently with backfill.

An entirely completed pass is **refreshed on the next invocation**, rather than
making historical days immutable. If today advances during recovery, the new
tail is fetched as well. Identical source snapshots are deduplicated; changed
service-response payloads retain separate raw snapshots. Stable-ID occurrences
are upserted; removed/replaced occurrences are marked noncurrent rather than
destroyed. Coverage, research and analytics exclude these noncurrent rows.
Without source entry IDs, the existing snapshot-position/content identity is
retained, with superseded rows noncurrent. Weight corrections update current
normalized weights while raw weight history remains intact. Raw snapshots are
the exact existing service-level JSON projection, not original GWT wire bytes.

Compatibility or serialization drift stops the pass and recommends
`uv run loseit-mcp compatibility-check`. It never rewrites configuration. Only
safe error types and recovery instructions are saved, not exception payloads or
credentials. Reports include requested/actual dates, completed/skipped/failed
chunks, diary/occurrence/raw/weight counters, standard coverage, research queue
count and database path. Counters describe completed chunks in this pass,
including resumed checkpoints; partial writes from an interrupted chunk are
idempotently reconciled on retry and are not a transaction-wide change audit.

## Local analytics API and exports

```bash
uv run loseit-sync --analytics --days 7
uv run loseit-sync --analytics --days 14 --json
uv run loseit-sync --analytics --days 30 --json
uv run loseit-sync --analytics --period week --json
uv run loseit-sync --analytics --period month --json
uv run loseit-sync --analytics --period ytd --json
uv run loseit-sync --analytics --start 2026-01-01 --end 2026-03-31 --json
# Optional explicitly chosen targets; no personal defaults exist:
uv run loseit-sync --analytics --days 7 --protein-target 100 --calorie-min 1800 --calorie-max 2200
# Separate opt-in name+brand grouping (default is stable source food ID):
uv run loseit-sync --analytics --days 30 --group-foods name --json
```

The example targets illustrate syntax only, not recommendations. All analytics
commands use SQLite read-only/query-only snapshots and never load authentication,
perform migrations, contact Lose It!, research food or write nutrition. Relative
windows use the local machine date; source dates are already local calendar
dates and are not reinterpreted as UTC. Calendar week is Monday through today;
calendar month and YTD end today. Custom dates are inclusive. Week/month bucket
summaries are clipped to the selected period. Comparisons use an equally long
preceding window; month mode uses the corresponding prior-month-to-date window,
capped at that month's end. Comparisons are local queries, never extra ingestion.

`analytics.read_analytics(data_dir, start, end, ...)` is the UI-independent service.
Schema 3 also provides `analytics_source_values` and `analytics_daily_source`
SQL views for current source observations. The Python service computes coverage,
portion-aware enrichment, calendar grouping, rankings and trends; future clients
should call it rather than duplicating those rules. Schema 1/2 databases remain
readable without migration; the views are installed on explicit writable use.

### Metric definitions and limitations

- Daily nutrient arrays are described by `daily_metric_fields`: source known
  sum, usable estimated gap-fill sum, combined known sum, source count, estimated
  count, missing count, and occurrence count. `null` means unavailable, never
  zero. Partial known sums remain visible with missing counts; source always wins.
- Logged day means at least one current food occurrence. Retrieved-empty days
  are not assumed fasting days. Logged-day means require full numeric nutrient
  coverage across logged days. Calendar-day intake means additionally require
  every date to be logged. `recorded_contribution_per_calendar_day` is reported
  separately: it is observed intake divided by calendar days, **not** an estimate
  of intake on unlogged days. A missing day is not silently assigned zero.
  `average_per_complete_logged_day` reports a separate complete-day subset mean
  alongside `complete_logged_days`, so partial periods still have an explicitly
  limited descriptive statistic rather than an implied complete-period mean.
- Numeric enrichment requires a reviewed high-confidence eligible reference
  and an explicit reviewed mapping in `nutrition_basis.occurrence_scaling`:
  `{"method":"logged_amount","unit":"serving"}`. Its unit must exactly match
  both the basis and stored occurrence unit. Values scale by logged amount /
  basis amount. No mapping is inferred from a food name, quantity or calories.
  Bounds-only or unscaled references can fill research **availability** while
  remaining unavailable for numeric totals; review flags expose this distinction.
- Meals preserve original classifications (including unknown/custom labels).
  Meal averages use distinct logged date/meal groups, not food occurrence count.
  Nutrient shares are withheld when their overall nutrient total is incomplete.
- Food ranking uses known combined contributions, with missing counts beside
  partial totals. Stable food IDs are not merged by similar names. Average
  portions require one known unit and all finite amounts. JSON indexes a shared
  top-food catalog to avoid repeating full records in every nutrient ranking.
- Weight statistics do not interpolate or assume a unit. Change, min/max and
  average require one known consistent unit. Daily recorded values retain their
  source unit, including null. Rolling observed-day averages require at least
  3 observations in 7 calendar days or 7 in 30, may include pre-period observations,
  and report their observation counts. Unknown/mixed units suppress trends.
- Consistency uses only complete logged days, population standard deviation and
  coefficient of variation (null with an unavailable/zero mean). Target adherence
  reports its eligible complete-day denominator, not an implied all-day rate.
  Logged-day streaks and missing days remain separate.
- Percentage comparisons are withheld if either period is incomplete or the
  prior denominator is zero. Coverage differences remain independently available.
- Data quality exposes source gaps, safely scaled enrichment, research workflow
  counts, absent IDs, and review-only source contradictions. Subnutrients exceeding
  parent nutrients and large calorie/macro discrepancies are heuristics, not
  corrections or proof of a bad label. No nutrient values are repaired automatically.

### Dashboard and workout-app readiness (no UI or remote integration yet)

The versioned export includes `period`, `logging_completeness`, `daily_metrics`,
`period_averages`, `weight_summary`, `meal_summary`, `food_contributors`,
`calendar_summaries`, `nutrient_coverage`, `consistency`, `weekday_weekend`,
`data_quality` and `comparison`. It omits raw diary snapshots, account metadata
and credentials. Daily arrays and shared food indexes reduce export size.

A future dashboard can consume these sections directly for overview cards,
calorie/protein trends, weight chart, macros, meals, food tables, coverage and
research panels. The research panel should use the existing research queue,
which remains authoritative for research availability.

`app_summary` is a small projection containing period length, logging completeness,
calorie/protein/fiber/sugar/sodium averages, protein adherence, weight change and
deterministic factual insight slots. Request a 7-day period for a 7-day app
summary. No medical advice or model-generated conclusions are inserted. No
CloudKit, remote API, dashboard framework or workout-app integration is installed.

The manual queue/proposal workflow remains available as an advanced recovery and
audit facility. It is no longer the normal path for configured evidence sources.

## Normal update workflow

1. Open the dashboard.
2. Click **Update My Nutrition Data**.
3. Review only items shown in **Needs Review**.

The same operation is available for manual or future scheduled execution:

```bash
uv run loseit-update
uv run loseit-update --json  # machine-readable completion summary
```

The operation preflights the saved session with two read-only calls, resumes from
the last successful sync checkpoint, retrieves diary and weight data in bounded
windows, updates raw snapshots and normalized records, checks the genuine
standard-nutrient gap queue, invokes the configured evidence worker, applies a
strict acceptance policy, then recalculates analytics. Both the dashboard and CLI
call `update.run_update`; there is no second sync implementation or background
polling. An OS file lock prevents concurrent update and backfill runs. Completed
sync windows and append-only stage events make retries idempotent and preserve
progress after an interruption.

The bundled `CuratedEvidenceWorker` is intentionally finite: it contains the four
previously researched references described below. This environment does not offer
a reliable external research API that the local dashboard can call, so unmapped
foods remain unresolved. No guessed or calorie-derived nutrition substitutes for
missing evidence. `ResearchWorker` is the explicit provider boundary for a future
source; it receives only stable food ID, normalized name and brand—not auth,
account identity, diary dates or raw logs.

### Research and automatic-acceptance policy

`exact-label-v1` accepts a missing-nutrient proposal only when all of these hold:

- stable source ID and normalized product/brand match exactly;
- evidence is a current (90 days or newer) manufacturer or retailer label;
- confidence is high and evidence values pass unit, finite-number, label-bound,
  and parent/subnutrient checks;
- every current source portion variant has an explicit matching unit and sane
  scale factor;
- calories and at least three existing non-calorie nutrients match the label at
  that exact scale within a deliberately narrow tolerance;
- no source conflict, stale context, conflicting proposal, manual flag, prior
  human rejection/defer decision, evidence mismatch or unexplained portion issue.

Anything else is append-only `Needs Review`; it is never silently discarded or
promoted. The policy fills only missing source nutrients through enrichment.
Source observations always win and retain their raw snapshot. Automatic decisions
carry `approval_actor=policy`; human approvals retain `manually_reviewed=1`, so
audit history never misrepresents automation as a person.

Bundled seed outcomes under this policy:

- Walmart Frosted Sugar Cookie: **Needs Review**. Eat This Much is secondary
  evidence, and calorie-only source matching cannot prove portion identity.
- McDonald's McCafé Iced Mocha Medium: **Needs Review**. The fingerprint matches,
  but CalorieKing is a secondary database rather than a direct product/retailer label.
- Great Value Sharp Cheddar: **eligible for automatic enrichment**. Instacart's
  retailer label matches the three-slice fingerprint and fills only 0 g carbohydrate.
- OREO Frozen Dessert Mini Cones: **Needs Review**. Kroger defines 220 calories as
  two cones/84 g while Lose It records one piece with the same fingerprint.
- Arnold Oatnut Bread: **Needs Review**. Most stored values scale like four slices
  while sodium scales like two; missing protein must not be automatically filled.

## Dashboard v1 (localhost only)

```bash
uv run loseit-dashboard
# Optional existing repository / unprivileged local port:
uv run loseit-dashboard --data-dir /private/path/to/repository --port 8501
```

Open `http://127.0.0.1:8501`. The launcher binds only to loopback, disables
Streamlit usage telemetry and file watching, keeps CORS/XSRF protection enabled,
limits uploads to 1 MB, disables static-directory serving and detailed error
traces, and uses an owner-only umask for new files. Use this launcher rather than
an unrestricted `streamlit run`. There is no public hosting, external font/chart
CDN, remote proposal API or background polling. **Refresh local data** reloads
SQLite only. **Update My Nutrition Data** is the only dashboard action that
explicitly contacts Lose It, through the existing read-only facade, and then runs
the controlled evidence worker. Other local processes/users with access to this machine
are not isolated by an application login: this is a single-user local tool, not
a multiuser server. Do not tunnel or reverse-proxy it to a public interface.

Streamlit was chosen to keep the dashboard in the existing Python/uv project:
its local forms, uploads, tables, charts and AppTest support avoid a separate
frontend build. Analytics pages import the reusable analytics, research queue,
proposal and repository services. Authentication is lazily loaded only after the
explicit Update action enters `update.live_service`; ordinary page loads do not
contact Lose It or a research provider. The connection badge reads only local
token expiry metadata and credential-free update history; there is no polling.

Normal use is intentionally short:

1. Usually do nothing; the Mac updates around 10:00 AM when available.
2. Optionally click **Update My Nutrition Data**.
3. Reconnect Lose It only when prompted.
4. Answer genuine ambiguities in **Review & Enrichment → Needs Your Help**.
5. Open the published read-only HTML from iCloud Drive on iPhone/iPad.

An interrupted update is safe to replay: completed date windows remain
checkpointed, and successful reconnection automatically starts the same
idempotent operation again.

### Daily scheduling and recent-day reconciliation

The dashboard can install, disable, re-enable, and uninstall a per-user macOS
LaunchAgent. It binds no network service and runs `loseit_mcp.scheduler` with the
same Python environment and repository path used at installation. `RunAtLoad`
provides post-login catch-up while `StartCalendarInterval` requests 10:00 AM.
The scheduler never asks macOS to wake the machine. The existing repository lock
prevents overlap with manual updates and historical backfills.

Every routine update re-reads **today plus the previous seven calendar days**,
even if those dates were imported or marked complete before. Added/deleted foods,
serving, meal, and nutrient changes update the current projection exactly once;
content-addressed raw snapshots remain immutable for audit. Unchanged payloads do
not create duplicate raw snapshots or current occurrences.

Advanced recovery commands (not required for normal use):

```bash
uv run loseit-schedule status
uv run loseit-schedule install
uv run loseit-schedule disable
uv run loseit-schedule enable
uv run loseit-schedule uninstall
```

Automatic stdout/stderr files contain only credential-free summaries and live in
the local repository's `logs/` folder.

### Explicit completed-day state

The stored real Lose It diary payloads and the currently used upstream SDK
projection expose no explicit “done logging”/day-complete field. The observed
top-level fields are date, entries/count, daily totals, total calories, and
nutrient-coverage metadata; nutrient coverage `complete` describes whether
entries reported a nutrient, not whether the user marked the day complete.
Therefore this project does **not** infer completion from date, calories, foods,
meals, time, or history.

Schema v7 is future-ready: it records an explicit recognized source boolean and
its field path per immutable raw snapshot if one appears later. Until then the
dashboard labels completion as unavailable and keeps the prior coverage-aware
insight behavior. When explicit state is available, live/current views still show
all entries while completeness-sensitive insights end at the latest explicitly
completed date. Rolling reconciliation still revisits completed days.

### Read-only iCloud snapshot

After a successful update and analytics refresh, publication writes only:

- `nutrition_dashboard.html` — one responsive, self-contained read-only page.
- `nutrition_snapshot.json` — a compact, versioned app-consumption document.

The destination must resolve inside the current account's iCloud Drive and is
configurable in **Settings & Targets**. Both outputs are staged and validated
before atomic replacement, so a generation failure leaves the previous good
snapshot intact. The export excludes the SQLite database, raw payloads, account
credentials, provider credentials, research attempts, proposal internals, and
administrative controls. The live dashboard remains bound to `127.0.0.1`; it is
never exposed publicly for mobile access.

JSON schema version 1 contains: generation/update freshness, selected period,
insight period, explicit-completion availability, logging completeness, five
core nutrient averages/coverage, recent daily usable totals and missing counts,
meal summaries, a bounded food summary, weight summary, factual insights, and
compact data-quality counts. Missing values remain JSON `null`, never zero.

### One-time nutrition research setup

USDA FoodData Central is the optional general research provider. In
**Settings & Targets → Nutrition Research**, follow **Get a free FoodData
Central API key**, paste the key into the masked field, and save it once. The
[USDA API guide](https://fdc.nal.usda.gov/api-guide/) says a data.gov key is
required on every request and must not be published. Its data are public
domain/CC0, the default limit is 1,000 requests per hour, and
[branded records](https://fdc.nal.usda.gov/data-documentation/) are updated
monthly from food-industry label data.

The provider sends only normalized food/brand search terms and accepts only an
exact normalized name plus exact brand owner/name for branded foods. Ambiguity,
conflicting identities, missing portion reconciliation, malformed data,
timeouts, and branded-to-generic substitutions fail closed. Evidence retains
its FoodData Central URL, retrieval date, product identity, 100 g basis,
nutrients, and source type. It does not weaken `exact-label-v1`: USDA database
evidence normally becomes a review proposal unless direct manufacturer/retailer
label requirements are independently met. Missing provider configuration never
blocks Lose It synchronization.

### Companion theme tokens

`companion_theme.json` is a standalone reference derived from workout-app commit
`6703248d`, file `MattFoleysMotivationalGym/AppTheme.swift`. It maps the app's dark
background/panel/panelRaised colors, pink/cyan/violet accents, lime/amber/danger
states, text hierarchy, 8-point corner radius, 16-point card padding and 14-point
spacing to dashboard tokens. `theme.py` validates the packaged values, supplies a
neutral accessible fallback, generates Streamlit CSS, and configures Altair axes
and palettes. The dashboard has no runtime dependency on the Swift repository and
the workout app is not modified.

### Sections and date filtering

- **Overview:** logging completeness; calories per logged day; protein and other
  nutrients per complete-nutrient day with sample sizes; recorded weight; research
  count; flagged-food count; macro-calorie coverage; factual observations.
- **Trends:** all nine nutrients, complete daily totals, amber partial-known
  points, seven-consecutive-complete-day averages and prior-period comparisons.
  Missing/incomplete dates break complete-value lines. Chart projections come
  from the reusable insights service, not chart-side arithmetic.
- **Meals:** original classifications, calories/protein, shares and averages per
  distinct logged date/meal group. Partial totals retain missing counts.
- **Foods:** all foods, most frequent, and six contributor rankings; name/brand
  search; source ID, occurrence count, source/enriched contributions and status.
  Default grouping preserves distinct source IDs. Optional normalized name/brand
  grouping is explicit. Search filters the selected table; choose All foods for
  a repository-period-wide search rather than just a top-ten list.
- **Weight:** recorded points, latest/count and valid min/max/rolling averages.
  "Weight unit unavailable from source" is displayed when appropriate. No lb/kg
  label or interpolation is invented; unit-dependent statistics remain unavailable.
- **Coverage & Data Quality:** source and numeric combined coverage, missing
  standard nutrients, enrichment-filled gaps, contradictions and manual flags.
  Unknown nutrients are tucked behind an explicit advanced-details action.
- **Review & Enrichment:** a default **Needs Your Help** inbox asks one factual
  quantity/identity question at a time, supports choices, numeric answers,
  “I don't know” and defer, and stores append-only provenance. Technical proposal,
  source annotation and evidence history remains available in a collapsed
  Advanced section.
  Queue counts are repository-wide;
  contradiction/source selectors use the selected analytics period (YTD for all
  current 2026 records).
- **Settings & Targets:** automatic-update install/status/disable controls,
  iCloud snapshot configuration, optional protein, calorie range, fiber, sodium upper
  target and sugar upper target. Empty is unconfigured; there are no personal
  defaults or recommendations. Adherence uses eligible complete days only. This
  section also holds the masked, one-time USDA provider configuration.

Presets: last 7/14/30 days, current month, YTD and custom inclusive dates. All
existing incomplete-data and local-calendar semantics still apply. In particular,
an unresolved reference is not necessarily missing nutrition. Research
availability and safely portion-scaled numeric coverage remain distinct.

### Advanced manual file → pending proposal → human decision

1. Run sync/backfill separately and export the standard research queue.
2. Research outside this application; have ChatGPT produce one proposal per food.
3. Open Review & Enrichment and upload the local JSON. Click **Import as pending
   proposal**. Upload alone never calls the approved-enrichment importer.
4. Inspect Needs Review or Ready to Approve. Compare stored source/context with
   proposed values, evidence, assumptions, uncertainty and nutrition basis.
5. Optionally edit the JSON and select only the nutrients to approve. Confirm
   product/evidence/units/basis verification. Conflicting proposals require a
   separate explicit acknowledgement; stale source contexts remain blocked.
6. Approve selected nutrients, reject, or defer. Successful decisions rerun the
   dashboard, immediately refreshing local analytics and queue state.

Approval creates a reviewed enrichment version through the existing stable-ID
importer, in the **same SQLite transaction** as the review event. It never
overwrites source nutrition. Source values still take precedence over estimates.
Partial approvals preserve compatible previously reviewed nutrients; remaining
proposal nutrients stay pending. Carrying prior nutrients across changed evidence,
confidence, assumptions, research date or portion basis is blocked rather than
silently relabelling their provenance. Use a fully reviewed replacement in that
case. Reject/defer never add nutrients. Duplicate exact proposals return the
existing proposal ID; conflicting overlapping proposals are flagged.

Edits and every decision retain their document, selected nutrients, note,
timestamp and user-edited marker. Proposal originals are immutable. History
shows review events plus enrichment versions with nutrient-level provenance.
Finalized approvals/rejections cannot be replayed; submit a new proposal for
later revisions. Existing command-line enrichment import remains available and
unchanged in purpose, but it is an **approved import**, not the pending UI workflow.

For contradictions, Needs Review shows stored portions, source values, current
flags, proposed mismatch explanations and evidence. Select a source record to
mark it unreliable, request review, or clear a prior annotation with a reason.
Annotations are append-only and exposed by analytics; they do not automatically
alter or suppress source numbers. Source identity/context conflicts that the
existing conservative importer cannot reconcile must be deferred or rejected,
not bypassed by checking a confirmation box.

### Proposal JSON v1

See `examples/proposal.schema.json` for the machine-readable structural schema.
Use one UTF-8 JSON object per file, at most 1 MB, containing:

- `schema_version: 1`
- `target`: copy the queue's entire `import_target`, including `source: "loseit"`,
  `source_food_id`, normalized name/brand and `source_context_sha256`.
- `food_name`, `brand`, `source_reference` (evidence title/name), `reference_url`,
  `match_type`, `confidence`, `assumptions`, `research_date` (ISO date).
- `nutrition_basis`: positive `amount`, exact `unit`, explanatory `description`;
  optional verified `occurrence_scaling` as documented under analytics.
- `nutrients`: one to nine unique standard nutrient records with `nutrient`,
  `unit`, `provenance`, and `estimated_value` and/or complete ordered
  `lower_bound`/`upper_bound`. Units must be kcal for calories, mg for sodium/
  cholesterol, and g for the other standard nutrients.
- Optional `notes` and `mismatch_explanation`.

Do not include `manually_reviewed`: the review service records human and policy
actors internally after a decision. Unknown
fields, duplicate JSON keys, unsupported/duplicate nutrients, missing provenance,
wrong units, negative/nonfinite/string/boolean numeric values, invalid bounds,
future research dates and credential-bearing/non-HTTP reference URLs are rejected.
Numeric magnitudes are capped at 1,000,000 per basis as an input safety limit,
not proof of nutritional plausibility. Human evidence/portion review remains
necessary. Runtime validation also checks live source ID/context, field equality,
bound ordering and unit mapping beyond the structural JSON Schema. Missing source
IDs are rejected; stale/mismatched current contexts are flagged and cannot be
approved. No-ID foods require separate conservative manual resolution in v1.

Reference URLs are displayed as data and never fetched by the app. Nothing in
the proposal format claims an external product match has been independently
verified by this application. Low-confidence approved versions remain labelled
and are not promoted to authoritative numeric data automatically.

### Persistence, insights and privacy

Schema 4 adds `research_proposals`, `proposal_reviews`, `source_review_flags` and
`dashboard_settings_versions`. All four are append-only, with SQL triggers
preventing updates/deletes. Settings store successive local snapshots, not browser
storage. Schema migration occurs only on an explicit local write action; opening
dashboard analytics against schema 1–3 remains read-only with no auth requirement.

Schema 5 adds append-only `update_events` and `research_decisions`, and records
the actor on proposal decisions and enrichment versions. Migration occurs only
during an explicit update/reconnect outcome or another explicit local write.

Schema 6 adds append-only `research_attempts`. It stores only stable source food
ID, provider, sanitized outcome/provenance, content hash, and timestamp—never a
provider key, Lose It credential, diary payload, meal, or account field.
Duplicate identical outcomes are ignored; update/delete triggers protect history.

`insights.generate_insights` produces up to five deterministic factual statements
using analytics outputs: configured protein adherence, valid prior-period calorie
changes, complete weekday/weekend comparison, incomplete fiber-day counts, top
protein contributions and logging completeness. It makes no model/network calls,
medical diagnoses or coaching recommendations. The same statements now populate
the existing `app_summary.insight_slots` for a future local workout-app adapter;
there is no remote integration in v1. Rolling chart values and additional target
adherence are reusable functions in the same service.

No credentials or raw diary payloads are sent to telemetry or debug logs. Uploaded
proposals stay local; data is stored in the existing private repository and never
committed. The launcher's error boundary avoids displaying diary-bearing tracebacks.
Private reports remain gitignored. Fixture research is never approved against a
real repository. Update errors are redacted at the worker/UI boundary. A failed
authentication preflight records only `reconnect_required`; a later chunk failure
preserves completed windows and records a credential-free failure status for safe
retry. No file/drop-folder watcher is installed.

## Analysis readiness

The schema supports calories/macros and other nutrient averages, meal-level
distribution and timing, contributor ranking, recurring foods, consistency,
weekday/weekend comparisons, weekly/monthly trends, source-versus-estimated
coverage, and weight relationships. Reports are intentionally not embedded in
the sync path; trustworthy collection and provenance come first.

## Scheduling later

No scheduler or cloud service is installed. A future weekly/monthly local
scheduler should call the same resumable operation as the dashboard:

```bash
cd /absolute/path/to/loseit-readonly && uv run loseit-update
```

`loseit-update` performs its own bounded windows and checkpoints. Re-running does
not duplicate diary, weight, proposal, decision or enrichment rows. Raw snapshots
are content-addressed, so an unchanged response is not duplicated while a changed
upstream day is preserved as a new snapshot. Unattended runs never prompt for
secrets: they exit with `reconnect_required` (CLI exit 3) when human browser
reconnection is needed, and continue syncing with research marked unavailable
when the optional provider is not configured. No scheduler is installed here.

## Privacy and backups

- Token files, `.env`, SQLite databases, WAL files, raw snapshots, and reports
  are ignored by Git.
- Keep the data directory on encrypted local storage.
- Back up the whole data directory while no sync is running, or use SQLite's
  `.backup` command for a consistent live backup.
- Store backups separately from the token. Treat both as sensitive.

## Testing

All tests use mocks or local SQLite databases; the default suite does not
contact Lose It! or use developer credentials.

```bash
uv run pytest
uv run ruff check src tests
```

The fork tests the exact allowlist, missing-nutrient behavior, range validation,
raw/enriched separation, enrichment provenance, idempotency, exact cache reuse,
fuzzy non-application, log redaction, and row sanitization. Seven inherited
tests for the removed hosted password-enrollment wiring are explicitly skipped;
the underlying helper modules are retained only to reduce rebase churn.

## Upstream updates and breakage

See [`UPSTREAM.md`](UPSTREAM.md) for audited revisions, fork changes, and the
rebase checklist. If reads suddenly fail with an incompatible-remote-service
error, Lose It! probably shipped a new GWT permutation or serialization policy.
Do not retry in a tight loop. Run the explicit, credential-free check:

```bash
uv run loseit-mcp compatibility-check
```

The command makes three read-only GETs for public deployment artifacts:
`web.nocache.js`, the Chromium cache bundle it selects, and that bundle's
`.gwt.rpc` policy. It confirms the bundle declares its own permutation, embeds
the `/web/service` proxy, and points to a policy containing the declarations
needed by the read-only bootstrap request. It does not load `liauth`, contact
`/web/service`, update configuration, or run during ordinary reads and syncs.

The upstream SDK's supported process remains authoritative: confirm the actual
`X-GWT-Permutation` header and the fifth pipe-delimited field of a captured
`/web/service` request, then follow its parser-schema regeneration runbook if a
new policy produces decoder/schema failures. Update defaults only after review;
then run the complete mocked suite and manually test `whoami`, `status`, one
search, one diary day, a two-day range, and a short weight range.

## Limitations

- The private interface is brittle and unsupported.
- A `liauth` token is powerful even though this program exposes only reads.
- Daily totals can be partial when entries omit nutrients; coverage metadata
  identifies this explicitly.
- Food-database entries are user-submitted and may be internally inconsistent.
- Fuzzy cache matching requires manual confirmation by design.
- Time-zone configuration uses Lose It!'s whole-hour offset model inherited
  from upstream; verify dates around travel and daylight-saving changes.

The source license remains 0BSD; the SDK retains its own MIT license terms.
