
# Quantified Self MCP
<img width="1771" height="608" alt="Gemini_Generated_Image_uohcyiuohcyiuohc" src="https://github.com/user-attachments/assets/79e7d666-bf04-41bd-9399-4c3bf3254983" />

> **Your health data. Your AI. Your machine.**

[![CI](https://github.com/Thecimal/quantified-self-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/Thecimal/quantified-self-mcp/actions)
[![PyPI](https://img.shields.io/pypi/v/quantified-self-mcp)](https://pypi.org/project/quantified-self-mcp/)
[![Python](https://img.shields.io/pypi/pyversions/quantified-self-mcp)](https://pypi.org/project/quantified-self-mcp/)
[![License](https://img.shields.io/github/license/Thecimal/quantified-self-mcp)](LICENSE)
[![Quantified Self MCP Server MCP server – quality and maintenance score on Glama](https://glama.ai/mcp/servers/Thecimal/quantified-self-mcp/badges/score.svg)](https://glama.ai/mcp/servers/Thecimal/quantified-self-mcp)

**Quantified Self MCP** is a privacy-first **Model Context Protocol (MCP) server** that gives AI agents controlled access to your personal **health data** stored locally.

Built with **Python, FastMCP, and SQLite**, it works with both **local LLMs and cloud-based LLMs**. You choose where your AI runs.

**[Try it on Glama →](https://glama.ai/mcp/servers/Thecimal/quantified-self-mcp)**

---

## What Is It?

Quantified Self MCP connects an AI agent to your personal health data through the **Model Context Protocol (MCP)**.

<img width="1920" height="1280" alt="Your machine
├── SQLite health database
├── Quantified Self MCP
└── AI client
       │
       ├── Local model → data stays local
       │
       └── Cloud model → queried health data may leave device" src="https://github.com/user-attachments/assets/315dac66-0af1-45ca-b6f4-709cdeabdb62" />

The MCP server does **not** require a specific AI provider.

You can run the entire AI stack locally, or connect the server to an online model when you prefer.

---

## 🏠 Local AI or ☁️ Cloud AI

The important distinction is between the **MCP server** and the **AI model**.

### Fully Local

<img width="1280" height="1920" alt="Your Health Data
       ↓
Local SQLite
       ↓
Quantified Self MCP
       ↓
Local AI Agent
       ↓
Local LLM" src="https://github.com/user-attachments/assets/1635fbb8-6d01-4d18-8546-989130df5598" />

With a local MCP-compatible agent and local LLM, your health data and AI inference can remain on your machine.

### Cloud LLM
<img width="1120" height="2240" alt="Your Health Data
       ↓
Local SQLite
       ↓
Quantified Self MCP
       ↓
AI Agent
       ↓
Cloud LLM" src="https://github.com/user-attachments/assets/b7372669-1832-43ff-8b24-686fabac2828" />

You can also connect the same MCP server to a hosted model.

In that setup, your **database and MCP server remain local**, while data returned by MCP tools may be sent to the cloud model provider.

**The choice is yours.**

Quantified Self MCP does not lock you into Claude, OpenAI, or any other model provider.

---

## 🔒 Privacy First

Your health data is stored locally in SQLite, and the MCP server runs on your machine.

The server itself does not require a cloud database, account, or hosted data store.

For maximum privacy, use a **local LLM** so the entire pipeline can remain on your machine.

```text
┌───────────────────────────────────┐
│          YOUR MACHINE             │
│                                   │
│          Health Data              │
│               ↓                   │
│          Local SQLite             │
│               ↓                   │
│      Quantified Self MCP          │
│               ↓                   │
│         Local AI Agent            │
│               ↓                   │
│           Local LLM               │
│                                   │
└───────────────────────────────────┘
```

### Optional Private Fields

If specific metrics should never be returned to the model, configure:

```bash
HEALTH_PRIVATE_FIELDS=weight_kg,mood
```

Private fields can still be stored and logged, but MCP read operations return them as `null`.

This gives you another layer of control over which health metrics an AI agent can access.

---

## ❤️ What Can It Track?

Quantified Self MCP currently supports:

* 👟 Daily steps
* 😴 Sleep duration
* ❤️ Resting heart rate
* ❤️ Heart rate
* 📈 Heart-rate variability (HRV)
* ⚖️ Weight
* 🏋️ Workout minutes
* 🙂 Mood
* 💧 Water intake

Every metric is optional, so you can track only the measurements you actually use.

---

## 💬 What Can You Ask?

Once connected to an MCP-compatible AI agent, you can ask questions naturally.

For example:

```text
How has my sleep changed over the last 30 days?
```

```text
What was my average step count this week?
```

```text
Show me my resting heart rate trend.
```

```text
How much water did I drink on average this month?
```

```text
What patterns do you see in my recent health data?
```

You can also log information through the AI agent:

```text
Log 7.5 hours of sleep for today.
```

Or correct a mistake:

```text
Clear today's mood entry.
```

---

## 🧠 MCP Tools

The server exposes **eighteen MCP tools**, organized in three layers:

**Layer 1 — Data**

| Tool                     | Purpose                                                                 |
| ------------------------ | ----------------------------------------------------------------------- |
| `read_health_data`       | Read all health metrics for a selected date range                       |
| `get_metric_history`     | Read a single metric's day-by-day values for a date range               |
| `log_daily_metric`       | Record one or more health metrics for a specific day                    |
| `clear_metric`           | Clear a single metric without affecting other data                      |
| `export_health_data_csv` | Write a date range of metrics to a local CSV file                       |
| `get_metric_provenance`  | Retrieve provenance information for a health metric and its source data |

`export_health_data_csv` writes straight to disk next to the database and returns only the file's path and a row count — not the row values themselves — so exporting a long history doesn't have to pass through a cloud LLM's context just to get a file you can open elsewhere.

**Layer 1b — Raw measurements & workout sessions**

| Tool                     | Purpose                                                                             |
| ------------------------ | ------------------------------------------------------------------------------------ |
| `log_measurement`        | Record one raw observation (metric, value, timestamp, source) instead of a day total |
| `read_measurements`      | Read individual measurement rows, most recent first, filterable by metric/source     |
| `log_workout_session`    | Record one workout as a structured event (activity, duration, intensity, heart rate) |
| `read_workout_sessions`  | Read individual workout sessions, most recent day first                              |
| `aggregate_measurements` | Roll up a day's raw measurements into that day's `daily_metrics` row for analytics    |

Use `log_measurement`/`log_workout_session` when the source, exact time, or multiple same-day readings matter (e.g. two wearables both logging heart rate); `aggregate_measurements` then folds those into `daily_metrics` so every Layer 2/3 tool below can use them.

### Tool routing

Which tool to call for a given request:

**Record data**

| User intent                                    | Tool                 |
| ----------------------------------------------- | --------------------- |
| Simple daily metric (steps, weight, mood, ...)  | `log_daily_metric`    |
| Individual timestamped/sourced measurement       | `log_measurement`     |
| Workout / exercise session                       | `log_workout_session` |

**Read data**

| User intent                          | Tool                   |
| ------------------------------------- | ----------------------- |
| Broad health data across metrics      | `read_health_data`      |
| Raw individual measurement rows       | `read_measurements`     |
| Workout / exercise sessions           | `read_workout_sessions` |
| One metric's history/trend over time  | `get_metric_history`    |

Each of these tools' own MCP description also states this explicitly ("Use this tool when" / "Do not use this tool when", with the alternative named), and the server's top-level MCP `instructions` repeat the same routing model — so the boundary is visible whether an agent reads one tool's schema or the whole tool list.

**Layer 2 — Analytics** (statistics computed over one or two metrics; see `analytics.py`)

| Tool                      | Purpose                                                        |
| ------------------------- | -------------------------------------------------------------- |
| `get_baseline`            | Mean/median/stdev for a metric over a window — "what's normal" |
| `detect_metric_anomalies` | Flag days that deviate sharply from a metric's own baseline    |
| `calculate_metric_trend`  | Fit a straight-line trend (direction, slope, r²) over a window |
| `compare_metric_periods`  | Compare a metric's average between two date ranges             |
| `find_metric_correlation` | Pearson correlation between two metrics, with optional lag     |

**Layer 3 — Personal intelligence** (composes Layer 2, returns facts rather than prose — the calling model still does the narration)

| Tool                    | Purpose                                                              |
| ----------------------- | -------------------------------------------------------------------- |
| `get_recent_changes`    | Scan every metric for notable shifts, anomalies, or trends recently  |
| `explain_metric_change` | Build an evidence bundle for "why did X look like that on this day?" |

The server also exposes read-only MCP resources for health metric schemas and individual days.

All data operations are scoped to the supported health metrics. The server does not expose arbitrary SQL execution to the model. Any metric listed in `HEALTH_PRIVATE_FIELDS` is refused by every Layer 2/3 tool outright (not just redacted afterward), since a baseline or anomaly computed from a private metric would leak its shape even without ever printing a raw value.

---

## 📚 Documentation Source of Truth

The MCP server implementation is the authoritative source for its available tools and schemas.

Because MCP clients and directories such as Glama inspect the running server directly, manually maintained tool lists can become outdated as new tools and metrics are added.

The project therefore treats the registered MCP tools and their schemas as the source of truth for tool documentation.

Tool documentation should be generated from the server's registered tools rather than maintained independently wherever practical.

A documentation check should ensure that:

```text
MCP Server
    ↓
Registered Tools
    ↓
Generated Documentation
    ↓
README / TOOLS.md
```

remain synchronized.

This prevents discrepancies between:

```text
Actual implementation
        ≠
GitHub documentation
        ≠
MCP directory inspection
```

and makes the available MCP interface easier for users, contributors, AI agents, and MCP directories to understand.

---

## 📥 Import Your Health Data

You can initialize the local database from CSV data.

```bash
quantified-self-init-db sample_data/health_sample.csv
```

The supported health fields include:

```text
date
steps
sleep_hours
resting_heart_rate
heart_rate
hrv_ms
weight_kg
workout_minutes
mood
water_ml
```

A handful of common alternate header spellings are also recognized
automatically, so you don't need to rename columns or know this
project's exact names first — e.g. `step_count`, `hr`, `bpm`, `weight`,
`sleep`, `hrv`. Anything else can still be mapped with `--map
COLUMN=HEADER` (see `init_db.py`'s `COLUMN_ALIASES` for the full alias
list, and its module docstring for `--map`).

You can also import an **Apple Health export**:

```bash
quantified-self-init-db export.xml
```

The importer maps supported Apple Health records into the local database.

**Android users**: Health Connect doesn't have a built-in export button
like Apple Health, so it needs one extra step — see
[docs/clients/android-health-connect.md](docs/clients/android-health-connect.md).

### See exactly what an import found, imported, skipped, and ignored

Add `--report` to any import to print a full breakdown instead of just a
one-line summary:

```bash
quantified-self-init-db export.xml --report
```

```text
IMPORT COMPLETE

Source: apple-health (export.xml)
Date range: 2025-03-01 -> 2026-09-13

Imported:
  Steps            420 day(s)
  Sleep            398 day(s)
  Heart Rate       410 day(s)
  HRV              180 day(s)
  Weight            30 day(s)

Skipped: 42 record(s)
Unsupported: 18 record type(s), 6,204 record(s) total
  HKQuantityTypeIdentifierBloodPressureSystolic: 3,102
  HKCategoryTypeIdentifierMindfulSession: 890
  ...
```

"Skipped" is records this import tried and failed to parse (bad
date/value — see the warnings printed alongside). "Unsupported" is
record types the importer doesn't map to any column at all — nothing
here is silently lost; it's counted and named so you know what a fuller
importer would need to add.

---

## ⚡ Installation

### PyPI

```bash
pip install quantified-self-mcp
```

This installs:

```text
quantified-self-mcp
quantified-self-init-db
```

### From Source

```bash
git clone https://github.com/Thecimal/quantified-self-mcp.git
cd quantified-self-mcp

python3 -m venv .venv
source .venv/bin/activate

pip install -r requirements.txt
```

### Docker

```bash
docker build -t quantified-self-mcp .
```

The included Docker configuration can be used for containerized MCP deployments, including Glama.

---

## 🚀 Quick Start

### 1. Install

```bash
pip install quantified-self-mcp
```

### 2. Load your health data

```bash
quantified-self-init-db your-health-data.csv
```

### 3. Connect the MCP server

Pick your client and follow the tested, step-by-step guide — each one takes you from a fresh clone to an answered question in about 5 minutes:

| Client | Guide |
| --- | --- |
| Claude Desktop | [docs/clients/claude-desktop.md](docs/clients/claude-desktop.md) |
| LM Studio (local models) | [docs/clients/lm-studio.md](docs/clients/lm-studio.md) |
| Open WebUI | [docs/clients/open-webui.md](docs/clients/open-webui.md) |

Any other MCP-compatible client works too — point it at `server.py` the same way, using the `.venv` Python interpreter.

### 4. Ask your health data questions

```text
How has my sleep changed over the last 30 days?
```

The AI agent retrieves the relevant health data through MCP and analyzes it.

---

## 🔌 MCP Client Compatibility

Quantified Self MCP uses the standard **Model Context Protocol**, so the server is designed to work with MCP-compatible clients and models rather than being tied to a single AI application.

The project includes configuration for clients supported by FastMCP, and standard MCP configuration can be generated for other compatible clients.

For local AI setups, pair the server with an MCP-compatible client and a local LLM runtime.

For example:

```text
Local LLM
   +
MCP-compatible Agent
   +
Quantified Self MCP
```

This allows the complete AI workflow to remain local.

---

## 🏗️ Architecture

```text
                         ┌────────────────────┐
                         │      AI Agent      │
                         └─────────┬──────────┘
                                   │
                              MCP Protocol
                                   │
                                   ▼
                         ┌────────────────────┐
                         │ Quantified Self    │
                         │       MCP          │
                         │                    │
                         │      FastMCP       │
                         └─────────┬──────────┘
                                   │
                                   ▼
                         ┌────────────────────┐
                         │    Local SQLite    │
                         │                    │
                         │    Health Data     │
                         └────────────────────┘
```

The **AI model and the MCP server are separate components**.

This means you can change the AI model without changing how your health data is stored or exposed.

---

## 🛠️ Technology

| Component        | Technology             |
| ---------------- | ---------------------- |
| Language         | Python                 |
| Protocol         | Model Context Protocol |
| MCP Framework    | FastMCP                |
| Database         | SQLite                 |
| Containerization | Docker                 |
| CI               | GitHub Actions         |
| Package          | PyPI                   |

---

## 🧪 Development

Clone the repository:

```bash
git clone https://github.com/Thecimal/quantified-self-mcp.git
cd quantified-self-mcp
```

Create a virtual environment:

```bash
python3 -m venv .venv
source .venv/bin/activate
```

Install dependencies:

```bash
pip install -r requirements-dev.txt
```

Run tests:

```bash
pytest
```

Build the package:

```bash
python -m build
```

GitHub Actions validates the project in a clean environment.

---

## 📁 Project Structure

```text
quantified-self-mcp/
├── .github/
│   └── workflows/
├── sample_data/
├── tests/
├── Dockerfile
├── fastmcp.json
├── glama.json
├── init_db.py
├── logic.py
├── import_adapters.py
├── server.py
├── pyproject.toml
├── requirements.txt
├── requirements-dev.txt
├── SECURITY.md
├── CONTRIBUTING.md
├── CODE_OF_CONDUCT.md
├── CHANGELOG.md
├── llms.txt
├── LICENSE
└── README.md
```

---

## 🛡️ Security

Health information is sensitive personal data.

Never commit:

* Personal health records
* Private SQLite databases
* API keys
* Passwords
* Authentication tokens
* Other sensitive personal information

For security vulnerabilities, please follow the instructions in [SECURITY.md](SECURITY.md).

---

## ⭐ Glama

Quantified Self MCP is available through the **Glama MCP directory**.

### Glama Score

**A / A / A**

| Category    | Score |
| ----------- | ----- |
| License     | **A** |
| Quality     | **A** |
| Maintenance | **A** |

The project is listed as a **Python / Local** MCP server on Glama. Glama performs its own inspection of the MCP server and may expose the current registered tools and schemas directly.

Because the server implementation is the source of truth, the Glama inspection may reflect newly registered tools or metrics before corresponding manually written documentation has been updated.

**[View Quantified Self MCP on Glama →](https://glama.ai/mcp/servers/Thecimal/quantified-self-mcp)**

---

## 🤝 Contributing

Contributions, bug reports, documentation improvements, and ideas are welcome.

Before contributing, please read:

* [CONTRIBUTING.md](CONTRIBUTING.md)
* [SECURITY.md](SECURITY.md)

If you find a bug, please open an issue with enough information to reproduce it.

---

## 📄 License

MIT License.

---

## Links

* **GitHub:** https://github.com/Thecimal/quantified-self-mcp
* **PyPI:** https://pypi.org/project/quantified-self-mcp/
* **Glama:** https://glama.ai/mcp/servers/Thecimal/quantified-self-mcp
* **Author:** https://github.com/Thecimal

---

> **Quantified Self MCP**
>
> **Your health data. Your AI. Your machine.**
