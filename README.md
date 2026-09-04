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
rebase checklist. If reads suddenly fail with an incompatible-remote-service or
decoder error, Lose It! probably shipped a new GWT permutation. Do not retry in
a tight loop. Compare current network requests with the constants in
`config.py`, update only after review, then run the complete mocked suite and
manually test `whoami`, one `search`, one `describe`, one diary day, a two-day
range, and a short weight range.

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
