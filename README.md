# Quantified Self MCP Server

A **local-first Model Context Protocol (MCP) server** for querying your personal health and finance data with an AI assistant.

Exposes your own SQLite databases (steps, sleep, resting heart rate, expenses) through MCP. Works with cloud models like Claude, or fully local models via Ollama. **No cloud database. No dashboard. No telemetry.**

## Registry & Demo
[![quantified-self-mcp MCP server](https://glama.ai/mcp/servers/Thecimal/quantified-self-mcp/badges/card.svg)](https://glama.ai/mcp/servers/Thecimal/quantified-self-mcp)

*View server health, inspector tools, and configuration options on Glama.*

## Features

- **Local-first** — your data never leaves your machine unless your chosen AI model is cloud-hosted
- **Read-only** — the server opens databases in read-only mode; it cannot modify your data
- **SQLite storage** — simple, portable
- **CSV import** with upsert (health) and append (finance) modes
- **Docker support**, MCP Inspector support for standalone testing

## Privacy Model

Three layers, each independently controlled:

| Layer | Where it runs | Notes |
|---|---|---|
| SQLite data | Your machine | Never uploaded or synced |
| MCP server | Your machine/container | Read-only access only |
| AI model | Local or cloud | **This is what determines where your data goes** |

With Claude Desktop, tool results are sent to Anthropic as part of the conversation. For a fully local, offline setup, pair this server with **Ollama** (or another local runtime) through an MCP client that supports it.

## Quick Start

```bash
git clone https://github.com/Thecimal/quantified-self-mcp.git
cd quantified-self-mcp
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Load sample data
python init_db.py health sample_data/health_sample.csv
python init_db.py finance sample_data/finance_sample.csv
```

Test it standalone with `fastmcp dev inspector server.py`, or connect it to an MCP-compatible client (see below).

## Using Your Own Data

**Health CSV** — columns: `date, steps, sleep_hours, resting_heart_rate`
**Finance CSV** — columns: `date, category, amount, description` (description optional)

```bash
python init_db.py health /path/to/health.csv
python init_db.py finance /path/to/finance.csv
```

- Dates: ISO (`YYYY-MM-DD`) or `MM/DD/YYYY`; amounts may include `$` and commas
- Invalid rows are skipped with a warning, not an aborted import
- Health data upserts by date (safe to re-run); finance data appends
- Add `--replace` to wipe and reload a dataset from scratch

## MCP Tools

| Tool | Purpose | Parameters | Default range |
|---|---|---|---|
| `read_health_data` | Steps, sleep, resting HR + min/max/avg | `start_date`, `end_date` | Last 30 days |
| `read_finance_data` | Expenses, categories, totals by category | `start_date`, `end_date`, `category` | Last 90 days |

All parameters optional. Example prompts: *"How has my sleep changed over the last 30 days?"*, *"How much did I spend on food this month?"*

## Using Claude Desktop

Add to your config file (macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`, Windows: `%APPDATA%\Claude\claude_desktop_config.json`, Linux: `~/.config/Claude/claude_desktop_config.json`, or via Settings → Developer → Edit Config):

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

Use the venv's Python path, not a bare `"python"` — Claude Desktop may not inherit your shell's `PATH`. Fully restart Claude Desktop after editing.

## Using Local Models (Ollama)

This server doesn't require Claude. Install Ollama, pull a model (e.g. `ollama pull qwen3:14b`), and connect it to this server through any MCP client that supports local model runtimes. For a fully offline setup: **Ollama (model) → MCP client → this server → local SQLite** — no cloud API involved.

## Docker

```bash
docker build -t quantified-self-mcp .
docker run -i --rm -v "$PWD/data:/app/data" quantified-self-mcp
```

Pure Python (`python:3.12-slim`), no Node.js required. Mount `data/` to persist your databases outside the container. Configurable via `HEALTH_DB_PATH` / `FINANCE_DB_PATH` env vars (defaults: `/data/health.db`, `/data/finance.db`).

## Security Notes

- Don't commit `data/*.db` or personal CSV exports (already gitignored)
- Treat MCP clients as trusted applications
- The server is intentionally read-only; `init_db.py` is the only component that writes to the databases
- Use a local model runtime if you need fully offline inference

## Troubleshooting

- **Server not showing up in Claude Desktop** — check the Python path is absolute, the venv exists, dependencies are installed, and you've fully restarted the app
- **"No health database found"** — run `init_db.py` first; the server won't auto-create empty databases
- **Changes to `server.py` not taking effect** — restart your MCP client, since most start the server process once per session

## Extending

Planned/possible additions: more metrics (weight, workouts, mood, nutrition...), analytical tools (trends, correlations, budget vs. actual), and eventually write tools (`log_expense`, `log_daily_metric`) — though these would need their own validation/authorization layer, kept separate from the current read-only design.

## Links

- [GitHub repository](https://github.com/Thecimal/quantified-self-mcp)
- [Glama listing](https://glama.ai/mcp/servers/Thecimal/quantified-self-mcp)

---

*Your data should be queryable by AI without handing the underlying dataset to a third party. MCP provides the interface, SQLite provides the storage, you choose the model.*

