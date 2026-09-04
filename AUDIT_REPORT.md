# Implementation and security audit

Date: 2026-09-04

## 1. Overall disposition

Implemented and locally verified against mocks and temporary SQLite databases.
No request was made to a real Lose It! account. The MCP discovery surface and
ordinary CLI are read-only by construction.

## 2. Requirement disposition

| Requested item | Disposition |
|---|---|
| Read diary, food data, and weight history | Complete |
| No AI-visible mutations | Complete; exact seven-tool allowlist plus startup assertion |
| Inclusive date range, maximum 31 days | Complete |
| Persistent raw/normalized/enriched repository | Complete; SQLite canonical plus raw JSON files |
| Weekly/monthly/manual readiness | Complete; idempotent CLI, no scheduler installed |
| Prefer session token and protect secrets | Complete; hidden token import, mode 0600, permissive modes rejected |
| Preserve food-text sanitization | Complete and regression-tested |
| Faithful missing nutrients | Complete; absence remains absence, coverage is explicit |
| Raw/source versus estimates | Complete; separate tables and append-only estimate versions |
| Research cache and conservative matching | Complete; exact and confirmed aliases apply, fuzzy only suggests |
| Derived-analysis-ready fields | Complete through normalized view and indexed observations |
| Local-first and no telemetry | Complete |
| Automated regressions | Complete |
| Upstream audit/maintenance guide | Complete in `UPSTREAM.md` |
| Documentation | Complete in `README.md` |

## 3. Files created or materially modified

Created: `AUDIT_REPORT.md`, `UPSTREAM.md`, `data/README.md`,
`examples/enrichment.json`, `src/loseit_mcp/readonly_service.py`,
`src/loseit_mcp/repository.py`, `src/loseit_mcp/sync_cli.py`, and
`tests/test_readonly_repository.py`.

Materially modified: `.env.example`, `.gitignore`, `README.md`, `Dockerfile`,
`docker-compose.yml`, `.github/workflows/ci.yml`, `pyproject.toml`, `uv.lock`,
package version/config/authentication, MCP registration, CLI, diary
serialization/read retry behavior, and affected upstream tests.

## 4. Runtime behavior

`loseit-mcp serve` starts a seven-tool MCP. `loseit-mcp import-token` accepts a
liauth token at a hidden terminal prompt. The read CLI supports identity,
status, search, describe, diary, diary range, and weights only.

`loseit-sync` fetches up to 31 diary days and weight history, adds exact raw
snapshots, updates the normalized projection idempotently, reuses only
high-confidence references, and queues unresolved foods. It does no web
research.

## 5. Read-only security audit

The actual registry is compared at server construction with:

`describe_food`, `get_diary`, `get_diary_range`, `get_weight_history`,
`search_food`, `server_status`, `whoami`.

Mutation names are absent from MCP discovery. The server returns no instance
with mutation methods to tool functions. The ordinary CLI has no log, custom
log, weigh, delete, enrollment, or secret-generation command. Entry IDs remain
informational.

Residual risk: Lose It! issues a bearer token with account-wide capability; it
does not offer a read-only token scope. The inherited internal service and SDK
contain mutation methods for upstream compatibility. A malicious local Python
program importing those internals is outside the MCP security boundary.

## 6. Authentication and logging audit

No credential is hard-coded or committed. The preferred token file defaults to
`~/.config/loseit-readonly/liauth`; atomic creation uses mode 0600 and reads
reject group/other permission bits. `.env`, token/session names, databases,
WAL files, snapshots, and reports are ignored.

Normal tool logging records tool name, duration, outcome, request/account tags,
and bounded error summaries. It does not log settings, passwords, cookies,
headers, diary payloads, or tokens. Tests verify settings redaction. The unused
upstream hosted-enrollment helper modules retain password handling but are not
wired into this build.

## 7. Nutrition repository architecture

SQLite is canonical. Raw diary and weight responses are also stored as
content-addressed JSON files with retrieval metadata. Normalized occurrences
and source nutrient observations are indexed for analysis. Research references,
aliases, enrichment versions, estimates, explicit occurrence links, and the
unresolved queue are separate. Reports/exports have reserved local directories.

## 8. Database schema summary

Migration/run audit: `schema_version`, `sync_runs`.

Raw: `raw_diary_snapshots`, `raw_weight_snapshots`.

Normalized: `food_occurrences`, `nutrient_observations`,
`weight_observations`, `normalized_food_records` view.

Enrichment: `food_references`, `food_aliases`, `enrichment_versions`,
`estimated_nutrients`, `occurrence_reference_links`, `enrichment_queue`,
`latest_enrichment_versions` view.

Foreign keys, timestamps, stable unique keys, content hashes, and common
date/food/nutrient indexes are present.

## 9. Testing performed

- Mocked/local test suite: 488 passed, 7 skipped.
- Static lint: passed.
- Distribution build: source archive and wheel succeeded.
- CLI help smoke tests: passed.
- Empty temporary repository smoke test: passed.
- MCP registry smoke test: exactly seven approved tools.
- Git whitespace validation: passed.

The seven skips are inherited tests for the removed hosted password-enrollment
wiring. No skipped test covers the read-only boundary or nutrition repository.

## 10. Limitations and unresolved issues

- No real account was contacted, as required.
- The private GWT-RPC interface can break on any Lose It! web release.
- Token scope is not enforceable at the Lose It! account level.
- Enrichment research is deliberate/manual; no arbitrary browsing is run.
- Aliases require a reviewed enrichment JSON import.
- Monthly periods longer than 31 days need two bounded syncs.
- Docker behavior is covered by CI configuration but was not required for the
  local-first workflow.

## 11. Regression audit

Regressions found during implementation:

1. Old tests expected mutation tools and commands. Root cause: intentional
   surface removal. Fix: changed those tests to assert absence and added a
   startup registry guard. Prevention: exact allowlist release gate.
2. Existing diary projection retained only common macros. Root cause: upstream
   optimized MCP payload size. Fix: retain every source-labelled nutrient.
   Prevention: missing/all-nutrient repository tests.
3. Initial range totals could be mistaken for complete data. Root cause:
   summing only observed values. Fix: per-nutrient coverage counts/completeness.
4. Initial unresolved queue count increased on a no-op re-sync. Root cause:
   matching ran after both inserts and conflicts. Fix: increment only for new
   occurrences. Prevention: idempotency regression.
5. Initial HTTP launcher used the wrong path keyword for MCP 2.0. Root cause:
   API keyword drift. Fix: `streamable_http_path`; CLI/package smoke checks.
6. Existing container/CI assumed password-based multi-tenant enrollment.
   Root cause: upstream deployment defaults. Fix: local single-account token
   mount and dummy-token health smoke configuration.

## 12. Commands to run next

See the README authentication section. In short:

```bash
uv sync
uv run pytest
uv run loseit-mcp import-token
uv run loseit-mcp whoami
uv run loseit-mcp status
uv run loseit-sync --days 7
uv run loseit-sync --show-queue
```

Do not run `import-token` until ready to configure the real account locally.

## 13. Git information

Upstream source: `https://github.com/cabird/loseit-mcp` at
`c1bf69cefb2c709688a517fe9b030ad2fe3f67f5`.

Implementation branch, commit hash, fork URL, and push status are recorded in
the delivery message because embedding the commit containing this report into
the report itself would change that commit hash.
