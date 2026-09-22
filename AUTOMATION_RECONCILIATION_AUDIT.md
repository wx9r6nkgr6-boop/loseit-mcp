# Daily automation, reconciliation, review, and mobile snapshot audit

Date: 2026-09-21

## Disposition by requested section

1. **Daily 10:00 AM Mac automation — implemented.** `scheduler.py` generates and
   manages a per-user LaunchAgent, uses the shared update/backfill lock and the
   authoritative update pipeline, gates successful catch-up to once per local
   date, and exposes install/status/disable/re-enable/uninstall in the dashboard.
   It neither requests wake nor exposes a network listener.
2. **Rolling reconciliation — implemented.** Routine updates always include today
   plus the prior seven dates. Current occurrences are reprojected; add/delete,
   serving, meal, and nutrient changes are counted; unchanged syncs are idempotent;
   immutable content-addressed raw history remains available.
3. **Completed-day investigation — no field observed.** Structural inspection of
   the real locally stored raw payloads found no explicit user day-completion
   state. `nutrient_coverage.*.complete` is nutrient presence, not user completion.
   No proxy was invented. Schema v7 has explicit-only, field-path-aware history
   and analytics endpoint support if Lose It exposes a boolean later.
4. **Automated review — improved without weakening policy.** Existing exact-label
   and USDA hierarchy remains in place, linked high-confidence decisions are
   reused, later successful evidence suppresses old provider exceptions, and
   answered identical question contexts are suppressed. Evidence thresholds are
   unchanged.
5. **Needs Your Help — implemented.** Default cards ask one factual quantity
   question and support discrete choice, positive numeric input, unknown, and
   defer. Answers are append-only and supplied as context to compatible research
   workers. Advanced proposal/provenance tools are collapsed by default.
6. **Responsive dashboard — implemented.** Fluid typography, wrapping, flexible
   columns, bounded form widths, responsive gaps, table overflow, chart bounds,
   and two width-specific refinements preserve the companion theme.
7. **Static iCloud snapshot — implemented.** Versioned JSON and self-contained
   HTML contain sanitized finished analytics only. Destination validation and
   staged atomic replacement preserve the prior good version on generation failure.
8. **Dashboard status — implemented.** Overview shows connection, diary/update/
   automatic-run status, next expected run, help count, publication state, and
   explicit completion availability without becoming a log console.
9. **Security/integrity — preserved.** No Lose It mutation path was added. Static
   output excludes credentials, raw payloads and research internals; localhost
   remains loopback-only; scheduled runs use the same fail-closed policy and lock.
10. **Migration — schema v7.** Additive/idempotent tables preserve all prior raw,
    normalized, enrichment, proposal, review, research, weight and update history.
11. **Tests — automated coverage added.** Tests cover LaunchAgent configuration,
    catch-up, daily gate, reconciliation add/change/delete/idempotence, raw history,
    explicit and unavailable completion, simple answers/duplicate suppression,
    responsive CSS, sanitized static outputs, and failed-export preservation.
12. **Real-data verification — recorded in delivery report.** Any unavailable
    authenticated/provider operation is reported separately from code validation.
13. **Documentation — updated.** README now separates the invisible normal flow,
    optional actions, help flow, mobile access, and advanced recovery commands.
14. **Final reporting — delivery message.** Branch, commit, push, concrete test and
    real-data results are reported after verification.

## Data-integrity notes

- Raw diary/weight snapshots remain immutable by triggers.
- Current occurrence semantics retain one active projection per latest day state;
  deleted entries become inactive and stop contributing to analytics.
- Completion, automation, publication settings/events and human answers are
  append-only.
- Static output is derived from analytics and never copies raw rows.

## Security notes

- MCP remains pinned to `describe_food`, `get_diary`, `get_diary_range`,
  `get_weight_history`, `search_food`, `server_status`, and `whoami`.
- The LaunchAgent plist contains paths and schedule metadata only.
- The management dashboard remains on `127.0.0.1`; mobile access is static iCloud
  output, not a public Streamlit deployment.
- The workout app was not modified. Physical iPhone/iPad/Watch deployment is not
  applicable to this pass.
