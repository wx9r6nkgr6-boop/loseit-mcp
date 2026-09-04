# Upstream audit and maintenance

## Audited revisions

- `cabird/loseit-mcp`: `c1bf69cefb2c709688a517fe9b030ad2fe3f67f5`
  (2026-08-01, “Make a production incident diagnosable”)
- `phitoduck/lose-it`: `fabf4fd211b80e149015ef62a71d68c38a151264`
  (2026-07-18, delete handling fix)

The SDK is resolved from the latter Git revision in `uv.lock`. Its public
`LoseIt` client includes reads (`search`, `diary`, food description) and writes
(`log_food`, `delete_entry`, backup restore). The upstream MCP service adds
custom-food logging and weight recording. This fork does not claim the SDK is
read-only; it prevents those capabilities from reaching MCP discovery or the
ordinary CLI.

## Intentional differences

- `server.py`: replaced registration with a seven-tool explicit allowlist,
  startup registry assertion, read-only instructions, preserved search-cell
  sanitization, and `get_diary_range`.
- `readonly_service.py`: new narrow facade with 31-day inclusive range
  validation and daily nutrient coverage/totals.
- `service.py`: diary reads use the read retry path and preserve every
  source-labelled nutrient rather than a macro-only subset.
- `cli.py`: removes log, custom-log, delete, weigh-in, enrollment, and secret
  generation commands; adds token import and range reads.
- `config.py`, `auth.py`: default owner-only liauth token file and permission
  checks; password-free setup is the documented default.
- `repository.py`, `sync_cli.py`: local SQLite/raw snapshot repository,
  idempotent sync, enrichment queue/cache, provenance, versioning, and weights.
- `.gitignore`, `.env.example`, `data/README.md`, `examples/`: personal-data and
  secret exclusions plus reviewed enrichment workflow.
- Tests: upstream mutation-surface expectations were changed to absence
  assertions; fork-specific integrity/security regressions were added. Hosted
  password-enrollment wiring tests are skipped because the route is not wired.

Legacy service mutation and hosted-enrollment helper modules remain in the
source tree only to keep upstream merging tractable. They are unreachable from
the MCP registry and read-only CLI. A future cleanup may delete them after the
fork has stabilized, but doing so increases long-term merge cost without
improving the enforced MCP boundary.

## Updating from upstream

1. Fetch upstream and create a dedicated update branch.
2. Review every new or changed SDK/MCP operation and network call. Never merge
   a generalized “register all methods” mechanism.
3. Rebase or merge upstream deliberately; resolve `server.py`, `cli.py`, and
   the facade in favor of the explicit read-only contract.
4. Inspect dependency changes and refresh `uv.lock`.
5. Run `uv run ruff check src tests` and `uv run pytest`.
6. Confirm MCP discovery is exactly `READ_ONLY_TOOL_ALLOWLIST`.
7. Search for mutation names and verify every occurrence is confined to
   inherited, unreachable implementation/tests/documentation.
8. With a fresh local token, manually exercise only the read smoke tests listed
   in the README. Never use live mutation tests against the personal account.

## Detecting private-interface changes

Likely signals are `IncompatibleRemoteServiceException`, HTML where GWT data
was expected, decoder field errors, or a sudden failure across several read
operations. Compare the current Lose It! web client's GWT strong name and
policy hash with `DEFAULT_STRONG_NAME` and `DEFAULT_POLICY_HASH`. Capture and
sanitize fixtures without cookies or JWTs, update parsers minimally, and add a
regression fixture before changing production configuration.

The read-only allowlist test is the release gate after every upstream update.
