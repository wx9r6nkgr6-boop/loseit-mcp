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

Normal synchronization uploads no diary contents to any third party. It only
contacts Lose It! and writes locally. There is no telemetry. Internet research
is not automatic.

## Install

Python 3.12+ and [`uv`](https://docs.astral.sh/uv/) are recommended.

```bash
git clone <your-fork-url> loseit-readonly
cd loseit-readonly
uv sync
uv run pytest
```

## Authentication

Session-token authentication is recommended; plaintext passwords are not
needed. Never paste credentials into an AI chat or put a token on a command
line, where shell history and process listings can expose it.

1. Sign in to `https://www.loseit.com/` in your browser.
2. Open browser developer tools, choose **Application** (Chrome/Edge) or
   **Storage** (Firefox), then **Cookies** → `https://www.loseit.com`.
3. Copy the value of the `liauth` cookie. It is a bearer credential.
4. From this repository, run:

   ```bash
   uv run loseit-mcp import-token
   ```

5. Paste at the hidden terminal prompt. The command writes
   `~/.config/loseit-readonly/liauth` with owner-only mode `0600`.
6. Verify without changing the account:

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

Repeat manually, weekly or monthly; no scheduler is installed. The research
queue itself opens SQLite in read-only/query-only mode, does not load credentials,
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

## Analysis readiness

The schema supports calories/macros and other nutrient averages, meal-level
distribution and timing, contributor ranking, recurring foods, consistency,
weekday/weekend comparisons, weekly/monthly trends, source-versus-estimated
coverage, and weight relationships. Reports are intentionally not embedded in
the sync path; trustworthy collection and provenance come first.

## Scheduling later

No scheduler or cloud service is installed. A weekly local scheduler can run:

```bash
cd /absolute/path/to/loseit-readonly && uv run loseit-sync --days 7
```

A monthly job should split months longer than 31 days into bounded calls or use
one exact 31-day range. Re-running the same dates does not duplicate diary or
weight rows. Raw snapshots are content-addressed, so an unchanged response is
not duplicated while a changed upstream day is preserved as a new snapshot.

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
