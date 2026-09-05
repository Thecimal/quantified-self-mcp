# Changelog

All notable changes to this project are documented here.

Format loosely follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versions correspond to the [PyPI release history](https://pypi.org/project/quantified-self-mcp/#history).

> **Note on the `v1.0.0` git tag:** it points to an early commit (Aug 29,
> before write tools, metric validation, PyPI packaging, or WAL hardening
> existed) and was never published to PyPI — `0.1.0` is the only version
> that has been. If you see `v1.0.0` in the repo's tag list, ignore it;
> `0.1.0` below is the actual first (and so far only) release.

## [Unreleased]

### Fixed
- `init_db.py` now only requires a `date` column in a CSV's header — it
  previously also required `steps`, `sleep_hours`, and `resting_heart_rate`
  to be present, which broke the documented "add one column later"
  incremental-update workflow (e.g. a follow-up CSV with just
  `date,weight_kg`) with a `SystemExit`.
- CI now builds the actual package (`python -m build`) and imports it from
  the built wheel, so a release missing a required module — as has
  happened twice before with `logic.py` — fails CI instead of reaching
  PyPI.

### Added
- `init_db.py` now supports a `--db-path` flag and reads the
  `HEALTH_DB_PATH` environment variable (matching `server.py`), so a
  `pip install`-ed copy can be pointed at a writable location instead of
  defaulting to somewhere inside the installed package.
- `tests/test_init_db.py`: unit tests for CSV parsing, row-skip handling,
  `--replace`, and the CLI — previously only exercised end-to-end by one
  happy-path CSV in CI, with no pytest coverage.

### Documentation
- README now documents installing from PyPI (`pip install
  quantified-self-mcp`) and configuring Claude Desktop for a pip install,
  alongside the existing source-checkout instructions.

## [0.1.0] - 2026-09-02

First published release ([PyPI](https://pypi.org/project/quantified-self-mcp/0.1.0/)).

- MCP server exposing local health data (steps, sleep, resting heart
  rate, weight, workout minutes, mood, water intake) to an LLM via
  FastMCP, backed by a local SQLite database.
- `read_health_data` and `log_daily_metric` / `clear_metric` tools, with
  range validation on logged values.
- `init_db.py` CSV importer with upsert-by-date semantics.
- WAL mode and a busy timeout on all SQLite connections, to reduce
  "database is locked" errors during concurrent access.
- `fastmcp.json` for one-command installs into Claude Desktop and other
  MCP clients.
- Automatic schema migration, so upgrading never requires deleting an
  existing database.
- Project narrowed from an original health-and-finance scope to
  health-only, to keep the initial release focused (finance tracking
  remains in git history for later).
