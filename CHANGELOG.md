# Changelog

All notable changes to this project are documented here.

Format loosely follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versions correspond to the [PyPI release history](https://pypi.org/project/quantified-self-mcp/#history).

## [Unreleased]

### Added
- New `export_health_data_csv` tool: writes a date range of health
  metrics to a CSV file on disk (next to the database) and returns only
  the file's path and a row count, rather than the row values
  themselves — so exporting a long history doesn't have to pass through
  a cloud LLM's context. Respects `HEALTH_PRIVATE_FIELDS` the same way
  `read_health_data` does.
- Layer 2/3 analytics tools: `get_metric_history`, `get_baseline`,
  `detect_metric_anomalies`, `calculate_metric_trend`,
  `compare_metric_periods`, `find_metric_correlation`, `get_recent_changes`,
  and `explain_metric_change` — see README.md's "MCP Tools" section. The
  underlying statistics (baseline, MAD-based anomaly detection, linear
  trend, period comparison, lagged Pearson correlation) live in the new,
  framework-free `analytics.py`, mirroring how logic.py separates
  MCP-independent logic from server.py's tool wiring. Every new tool
  refuses a metric listed in `HEALTH_PRIVATE_FIELDS` outright, the same
  guardrail `read_health_data` already applies, rather than merely
  redacting a value after computing something from it. `analytics.py` is
  now listed in `[tool.hatch.build.targets.wheel]`'s `include` so a
  `pip install` gets it too (the same class of bug the CI wheel-import
  check below already exists to catch).
- `tests/test_analytics.py`: unit tests for every function in
  `analytics.py`.
- `tests/test_mcp_client_integration.py`: integration tests for the new
  tools through the actual MCP client, plus a private-metric rejection
  test.
- `tests/test_logic.py`: real multi-threaded concurrency tests that hold
  a write lock on one connection while a second writes, and that fire
  several concurrent upserts at once — exercising WAL mode and
  `busy_timeout` under actual contention instead of only checking their
  pragma values.

### Changed
- CI's "Verify wheel contains all required modules" step now imports
  `analytics` explicitly alongside the existing modules.

## [0.2.0] - 2026-09-06

### Fixed
- `server.py` and `init_db.py` no longer default to storing `health.db`
  inside the installed package's own directory on a system-wide `pip
  install` (usually not writable). Both now detect whether `data/` next
  to the source is writable and fall back automatically to a per-user
  data directory (e.g. `~/.local/share/quantified-self-mcp` on Linux) —
  see `logic.default_data_dir`. `HEALTH_DB_PATH` and `--db-path` still
  override this either way.
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
