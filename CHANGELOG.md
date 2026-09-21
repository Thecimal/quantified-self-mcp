# Changelog

All notable changes to this project are documented here.

Format loosely follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versions correspond to the [PyPI release history](https://pypi.org/project/quantified-self-mcp/#history).

## [Unreleased]

### Fixed
- **Explicit `destructiveHint` on every tool** — ten tools omitted it, so
  MCP clients fell back to the spec default (`true`). All 18 tools now
  set all four annotation hints explicitly.
- **`export_health_data_csv` annotations** — it writes a CSV to disk, so
  it is now `readOnlyHint=False` (was `True`) and `idempotentHint=True`
  (was `False`; the filename is deterministic per date range, so repeat
  calls rewrite the same file rather than adding new ones).

### Added
- **Multi-metric `coverage` on `read_health_data`** — per-metric
  coverage percentages plus an overall `coverage_percent`/`confidence`
  for the whole date range, computed by the new
  `evidence.build_coverage_summary`, so a broad "what's my health
  data look like" read carries the same evidence signal the
  single-metric analytics tools already did.
- **`evidence` on every `get_recent_changes` change note** — each
  shift/anomaly/trend entry now carries its own coverage over the
  window it was computed from, closing the one analytics tool that
  previously reported findings with no coverage signal at all.
- Tool docstrings for `get_baseline`, `detect_metric_anomalies`,
  `calculate_metric_trend`, `compare_metric_periods`,
  `find_metric_correlation`, `get_recent_changes`, and
  `explain_metric_change` now explicitly instruct the calling model to
  fold `confidence`/`coverage_ratio` into how it phrases a result,
  rather than just returning the numbers alongside it.
- **`workout_sessions` table (schema v6)** — one row per workout instead
  of a single daily `workout_minutes` total: `activity_type`,
  `start_time`, `duration_minutes`, `intensity` (low/moderate/high),
  `avg_heart_rate`, `max_heart_rate`, `source`, `notes`. New
  `log_workout_session` / `read_workout_sessions` tools, and
  `explain_metric_change` now attaches the day's sessions (and a
  narrative fact per session) when explaining `workout_minutes`.
- **Provenance columns on `measurements` (schema v4)** — `importer` (which
  import path wrote the row, e.g. `"apple-health"`) and `imported_at`
  (when that import ran), alongside the existing `source`/`source_type`.
- **Apple Health import now records raw, source-tagged measurements** —
  each `sourceName` (e.g. "Ben's Apple Watch") is preserved per
  observation in `measurements`, not just blended into the daily
  aggregate. CSV import doesn't have a per-record source, so it still
  only writes `daily_metrics` as before.
- **`get_metric_provenance` tool** — breaks a metric's readings for a
  day down by source and flags a `conflict` when two sources disagree
  by more than a small tolerance, instead of silently averaging
  different devices together.
- **`resolve_source_conflicts` / `source_priority`** — `aggregate_measurements`
  now takes an optional ordered source list so a multi-source day
  aggregates from one chosen device rather than blending readings from
  different sources; without a priority, falls back to whichever source
  was imported most recently.
- **`measurements` table (schema v3)** — a raw, source-level layer under
  `daily_metrics`: one row per observation (`timestamp`, `metric`,
  `value`, `unit`, `source`, `source_type`), instead of one row per day.
  Lets a day hold several readings of the same metric (multiple
  workouts, repeated wearable samples) with enough context to explain a
  value, not just report it.
- **`log_measurement` / `read_measurements` / `aggregate_measurements`
  tools** — record a raw observation, read raw rows back with
  metric/date/source filters, and roll a day's raw measurements into its
  `daily_metrics` row (`logic.MEASUREMENT_AGGREGATION` decides sum vs.
  average vs. latest per metric) so existing analytics tools pick them
  up unchanged.

### Fixed
- **Every tool's "Privacy note" cloud-model warning was silently never
  reaching any real MCP client.** FastMCP derives a tool's client-facing
  `description` from its docstring via `inspect.getdoc()` + Griffe, which
  (a) dedents the docstring before parsing, so the warning's own 4-space
  indentation (copied from the source file's indentation) meant it could
  never substring-match a plain, undedented comparison string, and (b)
  only keeps the *leading* text block before `Args:` — a paragraph placed
  after `Returns:`, as every tool here had it, is parsed but then
  dropped, never making it into `description` at all. The existing test
  (`test_every_tool_carries_the_cloud_model_warning`) didn't catch this
  because it checked each function's raw Python `__doc__` rather than
  what `list_tools()` actually returns over the protocol — those two
  things are not the same, and diverged silently. Fixed by moving the
  warning to right after each tool's opening summary (before `Args:`)
  and rewriting `CLOUD_MODEL_WARNING` without the indentation that
  never survives dedenting. The test itself now asserts against
  `mcp.list_tools()`'s real `description` field, and discovers tools
  automatically instead of checking a hand-maintained tuple that had
  already silently missed several tools added after it was written.
  Verified against an actual `fastmcp.Client` round-trip, not just the
  server-side object, before and after.

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
