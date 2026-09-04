# Runtime data

This directory is a documented layout only. Its personal contents are ignored
by Git.

- `nutrition.sqlite3` — canonical SQLite repository
- `raw/` — immutable JSON diary and weight snapshots
- `normalized/` — optional portable normalized exports
- `enriched/` — optional portable enrichment exports
- `reference/` — reviewed research input documents
- `reports/` — generated weekly/monthly analysis

Back up the entire directory while `loseit-sync` is not running, or use
SQLite's `.backup` command. Keep backups encrypted because they contain health
and nutrition history.
