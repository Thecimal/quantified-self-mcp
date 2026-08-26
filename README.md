# Quantified Self MCP Server

A local [Model Context Protocol](https://modelcontextprotocol.io) (MCP) server that lets an LLM — e.g. Claude Desktop — query your personal health and finance data. Everything is stored in two local SQLite files and read directly off disk by a Python process you control. No cloud database, no dashboard, no third-party service.

## Registry & Demo
[![quantified-self-mcp MCP server](https://glama.ai/mcp/servers/Thecimal/quantified-self-mcp/badges/card.svg)](https://glama.ai/mcp/servers/Thecimal/quantified-self-mcp)

*View server health, inspector tools, and configuration options on Glama.*

## What's included

```
quantified-self-mcp/
├── server.py              # the MCP server (FastMCP) — 2 tools
├── init_db.py              # loads a CSV file into the local SQLite database
├── requirements.txt
├── .gitignore              # keeps data/ and .db files out of version control
└── sample_data/
    ├── health_sample.csv   # 30 days of sample data, so you can try it immediately
    └── finance_sample.csv  # ~2 months of sample expenses
```

Running `init_db.py` creates a `data/` folder next to `server.py` containing `health.db` and `finance.db` — that folder is not included here, since it's generated on your machine from your own data.

## Tools exposed

| Tool | Returns | Parameters (all optional) |
|---|---|---|
| `read_health_data` | Daily steps, sleep hours, resting heart rate | `start_date`, `end_date` (ISO `YYYY-MM-DD`; defaults to the last 30 days) |
| `read_finance_data` | Categorized expense ledger, with totals | `start_date`, `end_date`, `category` (defaults to the last 90 days, every category) |

Both tools return the matching rows *plus* computed summaries (averages/min/max for health, totals per category for finance), so the model doesn't have to do its own aggregation across many rows.

## 1. Set up the environment

Requires Python 3.10+.

```bash
cd quantified-self-mcp
python3 -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

## 2. Load your data

Try it immediately with the included samples:

```bash
python init_db.py health  sample_data/health_sample.csv
python init_db.py finance sample_data/finance_sample.csv
```

To use your own data, export it to CSV with these columns, then run the same commands against your files instead:

- **health CSV**: `date, steps, sleep_hours, resting_heart_rate`
- **finance CSV**: `date, category, amount, description` (`description` is optional)

Dates should be ISO format (`2026-08-23`); `MM/DD/YYYY` is also accepted and converted. Amounts/numbers may include `$` and `,` (e.g. `$1,234.56`) — those are stripped automatically. A row with a problem (bad date, non-numeric amount, missing category, etc.) is skipped with a warning rather than aborting the whole import; the last line printed always tells you how many rows loaded vs. were skipped.

Running `init_db.py health` again upserts by date (safe to re-run as you add days); `init_db.py finance` appends new rows each time, since a ledger has no natural unique key. Add `--replace` to either command to wipe the table first instead.

## 3. (Optional) test it on its own

Before wiring it into any client, you can open the MCP Inspector and call the tools directly in a browser:

```bash
fastmcp dev inspector server.py
```

## 4. Connect it to Claude Desktop

Claude Desktop launches local MCP servers as a subprocess and talks to them over stdio, based on a JSON config file:

- **macOS**: `~/Library/Application Support/Claude/claude_desktop_config.json`
- **Windows**: `%APPDATA%\Claude\claude_desktop_config.json`
- **Linux**: `~/.config/Claude/claude_desktop_config.json`

You can jump straight to it from the app: **Settings → Developer → Edit Config**.

Add an entry under `mcpServers`, using **absolute paths** — importantly, point `command` at the Python interpreter *inside the virtual environment you just created*, not a bare `python`. Claude Desktop runs servers in a minimal environment that doesn't reliably inherit your shell's PATH or an activated venv, so a bare `"python"` often resolves to the wrong interpreter (or none at all) and the server silently fails to start.

```json
{
  "mcpServers": {
    "quantified-self": {
      "command": "/absolute/path/to/quantified-self-mcp/.venv/bin/python3",
      "args": ["/absolute/path/to/quantified-self-mcp/server.py"]
    }
  }
}
```

On Windows, that's typically:

```json
{
  "mcpServers": {
    "quantified-self": {
      "command": "C:\\absolute\\path\\to\\quantified-self-mcp\\.venv\\Scripts\\python.exe",
      "args": ["C:\\absolute\\path\\to\\quantified-self-mcp\\server.py"]
    }
  }
}
```

Save the file, then **fully quit and reopen Claude Desktop** (not just close the window — restart is required to load config changes). Look for the hammer/tools icon in the chat box to confirm `quantified-self` is connected.

FastMCP also ships a CLI shortcut that edits this file for you — `fastmcp install claude-desktop server.py --name "Quantified Self"` — worth trying (run `fastmcp install claude-desktop --help` for current flags), but the manual JSON above will always work and is easier to debug if something's off. Anthropic also has a newer one-click "Desktop Extension" packaging format for local MCP servers; not necessary for personal use like this, but worth knowing about if you ever want to share this server with someone less comfortable editing JSON.

## 5. (Optional) run it in Docker / host it on Glama

[#5-optional-run-it-in-docker--host-it-on-glama](#5-optional-run-it-in-docker--host-it-on-glama)

A `Dockerfile` is included for anyone who wants to run this in a container instead of a local venv — including hosting it on [Glama](https://glama.ai), which builds directly from a repo's `Dockerfile` when one is present.

```
docker build -t quantified-self-mcp .
docker run -i --rm -v "$PWD/data:/app/data" quantified-self-mcp
```

The image is Python-only (`python:3.12-slim` + `pip install -r requirements.txt`); there's no Node.js anywhere in this project. `HEALTH_DB_PATH` and `FINANCE_DB_PATH` default to `/data/health.db` and `/data/finance.db` inside the container so a mounted volume (e.g. Glama's `/data` mount) persists your databases across redeploys — see the Configuration section at the top of `server.py` to override them.

`glama.json` is intentionally minimal — it just points Glama at this repo; the `Dockerfile` is the actual source of truth for how the image is built and started (`python server.py`, over stdio). An earlier version of `glama.json` tried to hand-configure a generic buildpack (a bare `debian:trixie-slim` base image plus manual `pip install` build steps and `cmdArguments`) instead of using a Dockerfile — that image had no Python interpreter reliably provisioned, and the platform fell back to trying to run a Node.js entrypoint that doesn't exist in this repo (`Cannot find module '/app/server.js'`). Shipping a `Dockerfile` removes that ambiguity.

## Privacy model — what "local" actually means

Worth being precise about this, since it's the whole point of the project:

- Both SQLite databases live only on your disk, inside this project's `data/` folder. The server makes no network calls, has no telemetry, and syncs nowhere.
- `server.py` opens both databases in SQLite's read-only mode (not just "doesn't issue writes" — the connection is physically unable to). Even a buggy or malicious prompt can't get either tool to modify your data; only `init_db.py`, run by you from the terminal, ever writes to them.
- When an MCP client calls one of these tools, *the specific rows returned for that query* become part of the conversation sent to whatever model is answering — that's the mechanism MCP uses to give a model information. If you're using Claude Desktop with a hosted model, that means whatever slice of data you ask about is sent to Anthropic for that turn, same as anything else you type into the chat.
- So "local" here means: your full dataset is never stored in, or synced to, any third-party database, and nothing is transmitted unless a tool is actually invoked — and even then, only the rows that specific call returns, not the whole database. It does not mean fully offline end-to-end. For that, you'd need a fully local model runtime (e.g. Ollama) paired with an MCP-compatible client.

## Troubleshooting

- **Server doesn't show up in Claude Desktop**: check `command` and `args` use absolute paths, confirm the venv's Python path actually exists, and confirm you fully quit and reopened the app. Logs live at `~/Library/Logs/Claude` (macOS) or `%APPDATA%\Claude\logs` (Windows) — `mcp-server-quantified-self.log` will show stderr from this server specifically.
- **"No health/finance database found" from a tool**: run `init_db.py` for that dataset first — the tools intentionally don't auto-create empty databases, so you don't get silently empty answers.
- **Edits to `server.py` don't seem to take effect**: restart Claude Desktop; it starts the server process once per app session, not per message.
- **Hosting on Glama fails with `Cannot find module '/app/server.js'`**: this means the deployment fell back to a Node.js runtime instead of Python — this repo has no `server.js`. Build from the included `Dockerfile` (see "Run it in Docker / host it on Glama" above) rather than a generic buildpack config, so the platform reliably runs `python server.py`.

## Extending this

A few natural next steps, if you want them — none of this is built, just where the pattern leads:

- Write tools (`log_expense`, `log_daily_metric`) so entries can be added through the LLM instead of the CSV/SQL directly.
- More metrics — weight, workouts, mood, water intake — each is just another table and another read tool.
- A budget-vs-actual tool that compares `read_finance_data` totals against targets you define.
