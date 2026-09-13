# Quantified Self MCP Server (Node/TypeScript port)

A local [Model Context Protocol](https://modelcontextprotocol.io) (MCP) server
that gives an LLM — e.g. Claude Desktop — read/write access to your own
health data: daily metrics, raw per-source measurements, and a small
analytics layer (baselines, anomaly detection, trend, correlation). Backed
by a local SQLite file only. No cloud database, no dashboard, no
third-party service.

This is a Node/TypeScript port of the Python original by
[Thecimal](https://github.com/Thecimal/quantified-self-mcp), matching
**v0.3.0**'s feature set. All credit for the original design goes to the
upstream project; this port is distributed under the same MIT license.

## Why a port, and not just a wrapper

The original ships as a Python (FastMCP) server. This port reimplements the
same tools and CSV/Apple-Health/Health-Connect import behavior natively in
TypeScript, so installing it via `npx` doesn't also require a Python
interpreter, a venv, or `pip install` — it's a single self-contained package.

**Known gap vs. the Python original:** the optional SQLCipher at-rest
encryption (`HEALTH_DB_PASSPHRASE`) isn't wired up in this port yet — plain
SQLite only. Use OS-level full-disk encryption (FileVault/BitLocker/LUKS) as
your baseline regardless, same as the original recommends either way.

## Quick start

```bash
npx quantified-self-mcp-node quantified-self-init-db sample_data/health_sample.csv
```

Or, once installed:

```bash
quantified-self-init-db sample_data/health_sample.csv
```

## Tools exposed

**Daily metrics (read/write)**

| Tool | Purpose |
| --- | --- |
| `read_health_data` | Daily steps, sleep, resting HR, weight, workout minutes, mood, water, heart rate, HRV — plus summary stats |
| `export_health_data_csv` | Write a date range to a CSV file on disk instead of returning every row |
| `log_daily_metric` | Record one or more metrics for a day (creates the row if needed) |
| `clear_metric` | Blank out a single metric for a single day |

**Raw measurements (source-level, one row per observation)**

| Tool | Purpose |
| --- | --- |
| `log_measurement` | Record a single timestamped observation with source/unit |
| `read_measurements` | Read back raw rows, filterable by metric/date/source |
| `aggregate_measurements` | Roll a day's measurements into its daily_metrics row |
| `get_metric_provenance` | Break a metric's readings for a day down by source (spot disagreements) |

**Analytics**

| Tool | Purpose |
| --- | --- |
| `get_metric_history` | One metric's day-by-day values |
| `get_baseline` | Mean/median/stdev over a window |
| `detect_metric_anomalies` | Median/MAD-based outlier detection |
| `calculate_metric_trend` | OLS slope/direction/r² over a window |
| `compare_metric_periods` | Compare one metric's average between two date ranges |
| `find_metric_correlation` | Pearson correlation between two metrics, with optional lag |

**Personal intelligence**

| Tool | Purpose |
| --- | --- |
| `get_recent_changes` | Scan every metric for period shifts, anomalies, and trends |
| `explain_metric_change` | Evidence bundle (baseline/anomaly/trend/correlations) for one day |

**Resources** (read-only, addressed by URI): `health://metrics/schema`
(valid range + privacy status per metric), `health://day/{date}` (one day's
snapshot, equivalent to `read_health_data` with a single-day range).

## Loading your data

```bash
quantified-self-init-db path/to/health.csv                # this project's CSV format
quantified-self-init-db path/to/export.xml                 # Apple Health export
quantified-self-init-db path/to/export.json --source health-connect
```

`--source` defaults to `auto` (`.xml` → Apple Health, anything else → CSV).

CSV columns (header names matched case-insensitively): `date` is required;
`steps, sleep_hours, resting_heart_rate, weight_kg, workout_minutes, mood,
water_ml, heart_rate, hrv_ms` are read if present. Differently-named headers
can be mapped instead of renamed:

```bash
quantified-self-init-db health.csv --map date=Date --map steps="Daily Steps"
```

Dates may be ISO (`2026-08-23`) or `MM/DD/YYYY`. Numbers may include `$`/`,`
(stripped automatically). Re-running **upserts by date** — safe to re-run as
you add days, or add a column later without blanking existing ones. Bad rows
are skipped with a warning rather than aborting the import. Pass `--replace`
to clear the table first instead of upserting.

Apple Health's `export.xml` (Health app → profile icon → Export All Health
Data) is parsed via streaming XML (safe for the very large files these
exports produce), aggregating step count, resting/active heart rate, HRV,
body mass, exercise time, dietary water, and sleep-analysis intervals per
calendar day. Health Connect's record-JSON export shape is also supported.
Mood has no HealthKit/Health-Connect identifier and must be logged
separately via `log_daily_metric`.

## Where data lives

Resolves relative to **the current working directory** you run commands
from — the standard convention for `npx`-distributed CLI tools — falling
back to a per-user data directory (e.g. `~/.local/share/quantified-self-mcp`
on Linux) if that isn't writable. Override with `HEALTH_DB_PATH` for a fixed
location regardless of cwd (same variable name the Python original reads).

## Privacy controls

- `HEALTH_PRIVATE_FIELDS` — comma-separated metric names (e.g.
  `weight_kg,mood`) that are always reported as `null` by every read tool
  and every write tool's own echoed-back row, and refused outright by every
  analytics tool (a baseline/trend computed from a "private" metric would
  leak its shape even with raw values redacted). The value is still written
  to the database — just never read back through any MCP tool.
- Every tool's description carries a privacy note: this server is entirely
  local, but the data a tool *returns* becomes part of whatever conversation
  the calling MCP client sends it in — treat that the same as pasting the
  data into a chat, if the model on the other end is cloud-hosted.
- Read tools (`read_health_data`, analytics) open the database read-only;
  only the write tools (`log_daily_metric`, `clear_metric`,
  `log_measurement`, `aggregate_measurements`) ever open a writable
  connection.

## Connect it to Claude Desktop

- **macOS**: `~/Library/Application Support/Claude/claude_desktop_config.json`
- **Windows**: `%APPDATA%\Claude\claude_desktop_config.json`
- **Linux**: `~/.config/Claude/claude_desktop_config.json`

```json
{
  "mcpServers": {
    "quantified-self": {
      "command": "npx",
      "args": ["-y", "quantified-self-mcp-node"],
      "env": {
        "HEALTH_DB_PATH": "/absolute/path/to/health.db"
      }
    }
  }
}
```

Fully quit and reopen Claude Desktop after saving. Look for the tools icon
in the chat box to confirm `quantified-self` is connected.

## Development

```bash
npm install
npm run build     # compiles TypeScript to dist/
npm start         # runs the server over stdio
```

## License

MIT — see [LICENSE](./LICENSE). Original project by
[Thecimal](https://github.com/Thecimal/quantified-self-mcp).
