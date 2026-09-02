# Quantified Self MCP

[![Glama MCP Server](https://glama.ai/mcp/servers/Thecimal/quantified-self-mcp/badge)](https://glama.ai/mcp/servers/Thecimal/quantified-self-mcp)

*Try it Live!*

**A private, local-first MCP server that lets LLMs access your personal health data.**

Quantified Self MCP connects an LLM to health data stored on your computer using the [Model Context Protocol (MCP)](https://modelcontextprotocol.io/).

It is **not limited to Claude**. It can work with local LLMs as well as cloud-based models that support MCP.

## Privacy first

Your health data is stored locally in SQLite, and the MCP server runs entirely on your computer.

```text
Your Health Data
      ↓
 Local SQLite
      ↓
  MCP Server
      ↓
   LLM
```

For maximum privacy, use a local LLM so everything stays on your machine.

Cloud LLMs such as Claude can also be used. In that case, your database and MCP server remain local, but the data returned to the model may be sent to the cloud provider.

## Current functionality

The server currently provides three tools:

**`read_health_data`** — read-only

It can access:

* Daily steps
* Sleep duration
* Resting heart rate
* Weight (kg)
* Workout minutes
* Mood (1–10 scale)
* Water intake (ml)
* Data for a selected date range

Every field is optional per day — log just the metrics you actually track.

**`log_daily_metric`** — write

Lets the LLM record any of the metrics above for a given day, without you touching a CSV or SQLite directly. Pass just the fields you're logging (e.g. only `mood`) and the rest of that day's data is left exactly as it was — nothing is ever cleared, only set. Values are checked against generous sanity bounds before being written (e.g. `mood` 1–10, `resting_heart_rate` 20–250 bpm) — this catches unit mix-ups and typos, not "abnormal" readings. It's a plain per-date upsert into `daily_metrics`; there's no way for it (or anything else in this server) to run arbitrary SQL. The same bounds are applied to CSV imports via `init_db.py`, so a bad value there is skipped with a warning rather than silently loaded.

**`clear_metric`** — write

Blanks out a single metric for a single day, for undoing a bad `log_daily_metric` call (wrong date, wrong units, etc.) without needing to re-run `init_db.py`.

## Installation

```bash
git clone https://github.com/Thecimal/quantified-self-mcp.git
cd quantified-self-mcp

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Initialize the database:

```bash
python init_db.py sample_data/health_sample.csv
```

CSV columns: `date, steps, sleep_hours, resting_heart_rate` are required;
`weight_kg, workout_minutes, mood, water_ml` are optional — include any
subset of them. Re-running `init_db.py` upserts by date, and a CSV that
omits an optional column leaves that column's existing values alone
rather than clearing them, so you can add a new metric later without
disturbing what's already logged. An existing database is migrated
automatically, so upgrading never requires deleting it.

## Using it with LLMs

Use it with any MCP-compatible client and model.

Examples:

* Local LLMs
* Claude
* Other MCP-compatible LLMs

Example:

> How has my sleep changed over the last 30 days?

The LLM retrieves the relevant data through MCP and analyzes it.

## Project structure

```text
server.py      # MCP server
logic.py       # Data validation and analysis
init_db.py     # Database initialization
sample_data/   # Example health data
```

## Philosophy

**Your data stays yours.**

Keep your personal data local, give the LLM controlled access, and choose whether the model runs locally or in the cloud.

## License

MIT
