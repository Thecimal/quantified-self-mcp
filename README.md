# Quantified Self MCP

> **Your health data. Your AI. Your machine.**

[![CI](https://github.com/Thecimal/quantified-self-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/Thecimal/quantified-self-mcp/actions)
[![PyPI](https://img.shields.io/pypi/v/quantified-self-mcp)](https://pypi.org/project/quantified-self-mcp/)
[![Python](https://img.shields.io/pypi/pyversions/quantified-self-mcp/0.2.2)](https://pypi.org/project/quantified-self-mcp/)
[![License](https://img.shields.io/github/license/Thecimal/quantified-self-mcp)](LICENSE)
[![Glama](https://img.shields.io/badge/Glama-A%20%2F%20A%20%2F%20A-blue)](https://glama.ai/mcp/servers/Thecimal/quantified-self-mcp)

**Quantified Self MCP** is a privacy-first **Model Context Protocol (MCP) server** that gives AI agents controlled access to your personal **health data** stored locally.

Built with **Python, FastMCP, and SQLite**, it works with both **local LLMs and cloud-based LLMs**. You choose where your AI runs.

**[Try it on Glama →](https://glama.ai/mcp/servers/Thecimal/quantified-self-mcp)**

---

## What Is It?

Quantified Self MCP connects an AI agent to your personal health data through the **Model Context Protocol (MCP)**.

```text
                 ┌─────────────────────┐
                 │      AI Agent       │
                 │                     │
                 │ Local LLM / Cloud   │
                 └──────────▲──────────┘
                            │
                     MCP tool result
                            │
                     MCP tool call
                            │
                 ┌──────────┴──────────┐
                 │ Quantified Self MCP │
                 │      FastMCP        │
                 │       LOCAL         │
                 └──────────▲──────────┘
                            │
                       SQL / data
                            │
                 ┌──────────┴──────────┐
                 │    Local SQLite     │
                 │     Health Data     │
                 │       LOCAL         │
                 └─────────────────────┘
```

The MCP server does **not** require a specific AI provider.

You can run the entire AI stack locally, or connect the server to an online model when you prefer.

---

## 🏠 Local AI or ☁️ Cloud AI

The important distinction is between the **MCP server** and the **AI model**.

### Fully Local

```text
Your Health Data
       ↓
Local SQLite
       ↓
Quantified Self MCP
       ↓
Local AI Agent
       ↓
Local LLM
```

With a local MCP-compatible agent and local LLM, your health data and AI inference can remain on your machine.

### Cloud LLM

```text
Your Health Data
       ↓
Local SQLite
       ↓
Quantified Self MCP
       ↓
AI Agent
       ↓
Cloud LLM
```

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

The server currently provides four MCP tools:

| Tool                     | Purpose                                                    |
| ------------------------ | ----------------------------------------------------------- |
| `read_health_data`       | Read health metrics for a selected date range                |
| `log_daily_metric`       | Record one or more health metrics for a specific day         |
| `clear_metric`           | Clear a single metric without affecting other data            |
| `export_health_data_csv` | Write a date range of metrics to a local CSV file             |

`export_health_data_csv` writes straight to disk next to the database and returns only the file's path and a row count — not the row values themselves — so exporting a long history doesn't have to pass through a cloud LLM's context just to get a file you can open elsewhere.

The server also exposes read-only MCP resources for health metric schemas and individual days.

All data operations are scoped to the supported health metrics. The server does not expose arbitrary SQL execution to the model.

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
weight_kg
workout_minutes
mood
water_ml
```

You can also import an **Apple Health export**:

```bash
quantified-self-init-db export.xml
```

The importer maps supported Apple Health records into the local database.

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

Connect Quantified Self MCP to an MCP-compatible AI agent.

### 4. Choose your model

Use either:

* **A local LLM**
* **A cloud-based LLM**

### 5. Ask your health data questions

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

**A / A / B**

| Category    | Score |
| ----------- | ----- |
| License     | **A** |
| Quality     | **A** |
| Maintenance | **B** |

The project is listed as a **Python / Local** MCP server on Glama, and its current MCP inspection shows three health-data tools with maintained activity.

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
