# Quantified Self MCP
*Try it Live!*

[![Glama MCP Server](https://glama.ai/mcp/servers/Thecimal/quantified-self-mcp/badge)](https://glama.ai/mcp/servers/Thecimal/quantified-self-mcp)



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

### Option A: from PyPI (recommended)

```bash
pip install quantified-self-mcp
```

This installs two commands: `quantified-self-mcp` (the server itself) and
`quantified-self-init-db` (the CSV loader below). By default both store the
database inside wherever pip installed the package (not your current
directory), which usually isn't writable on a system-wide install. Point
them somewhere you control with the `HEALTH_DB_PATH` environment variable
(read by both), or pass `--db-path` to `quantified-self-init-db` directly —
see [Limitations](#limitations) for details.

Initialize the database:

```bash
quantified-self-init-db sample_data/health_sample.csv --db-path ~/quantified-self/health.db
```

(No sample CSV handy from a `pip install`? Grab it from the repo:
`curl -O https://raw.githubusercontent.com/Thecimal/quantified-self-mcp/main/sample_data/health_sample.csv`)

### Option B: from source

Use this if you want to read/modify the code, or run the test suite.

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

CSV columns: `date` is the only one required; `steps, sleep_hours,
resting_heart_rate, weight_kg, workout_minutes, mood, water_ml` are all
read only if present — include any subset of them. Re-running `init_db.py`
upserts by date, and a CSV that omits a column leaves that column's
existing values alone rather than clearing them, so you can add a new
metric later (even one from the original four) without disturbing what's
already logged. An existing database is migrated automatically, so
upgrading never requires deleting it.

## Using it with LLMs

Use it with any MCP-compatible client and model — local LLMs, Claude, or anything else that speaks MCP.

### Claude Desktop

**If you installed from source**, one command, using the included `fastmcp.json`:

```bash
fastmcp install claude-desktop
```

This registers the server in Claude Desktop's config, and has `uv` manage an isolated environment with this project's dependencies (no need to have already run `pip install -r requirements.txt` first) — restart Claude Desktop afterwards and look for the 🔨 icon to confirm it loaded.

**If you installed via `pip install quantified-self-mcp`**, edit the config file directly instead — `fastmcp install` expects a source checkout, not a pip package. Find `command` by running `which quantified-self-mcp` (macOS/Linux) or `where quantified-self-mcp` (Windows), then add:

```json
{
  "mcpServers": {
    "quantified-self": {
      "command": "/absolute/path/from/which/quantified-self-mcp",
      "env": {
        "HEALTH_DB_PATH": "/absolute/path/to/health.db"
      }
    }
  }
}
```

Set `HEALTH_DB_PATH` here to match whatever `--db-path` you used when running `quantified-self-init-db` — otherwise the server falls back to its own default location and won't see the data you just loaded.

into the same config file the source-install instructions above point to (**macOS**: `~/Library/Application Support/Claude/claude_desktop_config.json`, **Windows**: `%APPDATA%\Claude\claude_desktop_config.json`, **Linux**: `~/.config/Claude/claude_desktop_config.json`). Restart Claude Desktop afterwards.

### Other MCP clients (Cursor, Claude Code, Gemini CLI, etc.)

```bash
fastmcp install cursor        # or: claude-code, gemini-cli, goose
```

Any client not directly supported by `fastmcp install` can still use standard MCP JSON config, generated the same way:

```bash
fastmcp install mcp-json fastmcp.json
```

Paste the output into that client's config file under its `mcpServers` key.

### Try it

> How has my sleep changed over the last 30 days?

The LLM retrieves the relevant data through MCP and analyzes it.

## Project structure

```text
server.py       # MCP server
logic.py        # Data validation and analysis
init_db.py      # Database initialization
sample_data/    # Example health data
fastmcp.json    # One-command install into Claude Desktop/Cursor/etc.
CHANGELOG.md    # Release history
```

## Limitations

**Single machine only, by design.** The database is a plain SQLite file on disk — there's no sync, no server component, no accounts. That's the same choice that keeps your data private: nothing here is built to talk to a network. If you use this on more than one computer, each one has its own independent `data/health.db`; nothing here merges them. Copying the file yourself (e.g. via a synced folder) works but isn't something this project manages or is tested against.

**`pip install` default database location.** Both commands default to storing `health.db` inside the installed package's own directory (wherever `pip` put it), not your current directory or home folder — that's rarely writable on a system-wide install. Use `HEALTH_DB_PATH` (read by both `quantified-self-mcp` and `quantified-self-init-db`) or `quantified-self-init-db`'s `--db-path` flag to point it somewhere you control, e.g. `~/quantified-self/health.db`.

## Philosophy

**Your data stays yours.**

Keep your personal data local, give the LLM controlled access, and choose whether the model runs locally or in the cloud.

## License

MIT
