<div align="center">

# Quantified Self MCP

**Your health data. Your AI. Your machine.**

A privacy-first, local Model Context Protocol (MCP) server that connects AI assistants to your personal health history — with **statistical evidence, provenance, and granular privacy** built in.

<p align="center">
  <a href="https://github.com/Thecimal/quantified-self-mcp/actions/workflows/ci.yml"><img src="https://github.com/Thecimal/quantified-self-mcp/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="https://github.com/Thecimal/quantified-self-mcp/actions/workflows/codeql.yml"><img src="https://github.com/Thecimal/quantified-self-mcp/actions/workflows/codeql.yml/badge.svg" alt="CodeQL"></a>
  <a href="https://pypi.org/project/quantified-self-mcp/"><img src="https://img.shields.io/pypi/v/quantified-self-mcp.svg" alt="PyPI"></a>
  <a href="https://pypi.org/project/quantified-self-mcp/"><img src="https://img.shields.io/pypi/pyversions/quantified-self-mcp.svg" alt="Python Versions"></a>
  <a href="https://github.com/Thecimal/quantified-self-mcp/blob/main/LICENSE"><img src="https://img.shields.io/github/license/Thecimal/quantified-self-mcp" alt="License"></a>
  <a href="https://glama.ai/mcp/servers/Thecimal/quantified-self-mcp"><img src="https://glama.ai/mcp/servers/Thecimal/quantified-self-mcp/badges/score.svg" alt="Glama Score"></a>
  <a href="https://m8ven.ai/mcp/thecimal-quantified-self-mcp-v6tlvp"><img src="https://m8ven.ai/badge/mcp/thecimal-quantified-self-mcp-v6tlvp" alt="M8ven Score"></a>
  <a href="https://github.com/astral-sh/ruff"><img src="https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json" alt="Ruff"></a>
</p>

<p align="center">
  <a href="#-instant-client-setup"><b>⚡ Instant Client Setup</b></a> •
  <a href="#-quick-start"><b>🚀 Quick Start</b></a> •
  <a href="#-why-quantified-self-mcp"><b>💡 Why This?</b></a> •
  <a href="#-evidence--provenance-engine"><b>🔎 Evidence Engine</b></a> •
  <a href="#-supported-data--integrations"><b>📊 Supported Data</b></a> •
  <a href="docs/"><b>📚 Documentation</b></a>
</p>

<img width="1536" height="1024" alt="Quantified Self MCP Hero" src="https://github.com/user-attachments/assets/447e8018-b56f-4dfb-bfa9-909e9ad0de6f" />

</div>

---

## ⚡ Instant Client Setup

Connect Quantified Self MCP to your favorite AI assistant in seconds:

### Claude Desktop

Add this to your Claude Desktop configuration:
* **macOS**: `~/Library/Application Support/Claude/claude_desktop_config.json`
* **Windows**: `%APPDATA%\Claude\claude_desktop_config.json`
* **Linux**: `~/.config/Claude/claude_desktop_config.json`

```json
{
  "mcpServers": {
    "quantified-self": {
      "command": "uvx",
      "args": ["quantified-self-mcp"]
    }
  }
}
```
*(Or point `"command"` to your installed `quantified-self-mcp` binary if installed via pip).*

### Cursor

In Cursor, navigate to **Settings → Features → MCP Servers → Add New MCP Server**, or add to `.cursor/mcp.json`:

```json
{
  "mcpServers": {
    "quantified-self": {
      "command": "uvx",
      "args": ["quantified-self-mcp"]
    }
  }
}
```

### Windsurf

Add to your `mcp_config.json`:

```json
{
  "mcpServers": {
    "quantified-self": {
      "command": "uvx",
      "args": ["quantified-self-mcp"]
    }
  }
}
```

> [!TIP]
> **Need guides for LM Studio, Open WebUI, or Android Health Connect?**
> See our step-by-step [Client Guides](docs/clients/).

---

## 🚀 Quick Start

### 1. Install

```bash
pip install quantified-self-mcp
```

### 2. Import Your Health History

Quantified Self MCP automatically creates and manages a local SQLite database (`health.db`):

```bash
# From CSV:
quantified-self-init-db your-health-data.csv

# From Apple Health export:
quantified-self-init-db export.xml

# From WHOOP Developer API (v2) JSON:
quantified-self-init-db whoop.json --source whoop

# From Android Health Connect export:
quantified-self-init-db health_connect_export.zip
```

### 3. Verify Setup

Run the built-in diagnostic tool to verify Python dependencies, schema integrity, and database readiness:

```bash
quantified-self-doctor
```

### 4. Ask Your AI

Open your MCP-connected assistant and ask questions in plain English:

```text
"How has my sleep and resting heart rate changed over the last 30 days?"
"Why did my HRV drop last week?"
"What patterns exist between my workout minutes and sleep quality?"
"Which of my health metrics changed the most this month?"
```

---

## 💡 Why Quantified Self MCP?

Most health apps either trap your data in a closed cloud dashboard or feed raw numbers blindly into an LLM. Quantified Self MCP provides a **trustworthy, analytical bridge**:

| Feature | Standard Health Dashboards | Naive LLM Prompts | Quantified Self MCP |
| :--- | :---: | :---: | :---: |
| **Data Ownership** | ❌ Locked in Cloud | ⚠️ Uploaded to 3rd-party | ✅ **100% Local SQLite** |
| **Privacy Redaction** | ❌ All or nothing | ❌ Raw data dumped | ✅ **Granular Field Exclusion** |
| **Statistical Claims** | ⚠️ Uncalibrated percentages | ❌ Hallucinated correlations | ✅ **ClaimEvidence & Caveats** |
| **Provenance Tracking** | ❌ Opaque | ❌ None | ✅ **Multi-source origin stamps** |
| **Atomic Snapshots** | ⚠️ Manual exports | ❌ None | ✅ **WAL-safe backup/restore** |

### 🔒 100% Local & Private
Your database lives on your machine in SQLite. No proprietary health cloud required. If you want a completely offline, air-gapped system, pair it with local models via **LM Studio** or **Ollama**.

You can also redact specific sensitive metrics from being exposed to the AI:
```bash
export HEALTH_PRIVATE_FIELDS=weight_kg,mood
```
*Private fields remain safely stored in SQLite for your own records, but are blocked from all AI queries and analytics.*

---

## 🔎 Evidence & Provenance Engine

When an AI analyzes your health, **a number without context is dangerous**.

If an LLM sees a 24% drop in HRV, did it come from 30 continuous days of wearable data, or just 4 sparse readings with a week-long gap?

Quantified Self MCP solves this with an integrated **ClaimEvidence Engine**:

```text
Analysis Result
     ↓
claim: ClaimEvidence
     ├── evidence   → Raw coverage observed (days logged, max gap, freshness)
     ├── profile    → Dimension assessment (sample size, temporal span, missingness)
     └── decision   → Authoritative claim tier:
                      [ insufficient | suggestive | detectable_not_meaningful | supported ]
                      + specific mandatory caveats (must_state)
```

### Example: Naive vs. Evidence-Backed AI Response

* **Without Evidence Engine (Naive LLM)**:
  > *"Your HRV dropped 24% over the last month. You may be overtraining."*

* **With Quantified Self MCP Evidence**:
  > *"Your HRV decreased 24% over the last 30 days — but HRV was only logged on 71% of those days with a 5-day recording gap, so this is flagged as suggestive rather than a confirmed trend. (Source: Apple Watch)."*

Every analytical tool (`calculate_metric_trend`, `explain_metric_change`, `detect_metric_anomalies`) returns explicit claim tiers alongside mandatory disclosures.

---

## 📊 Supported Data & Integrations

Quantified Self MCP ships with 20 specialized tools spanning raw measurements, longitudinal statistics, and data health:

```text
                  AI Agent (Claude / Cursor / LM Studio)
                                   │
                              MCP Protocol
                                   │
                                   ▼
          ┌──────────────────────────────────────────────────┐
          │               Quantified Self MCP                │
          │                                                  │
          │  • Data Access         • Longitudinal Analytics  │
          │  • Workout Tracking    • ClaimEvidence Engine    │
          │  • Provenance Resolver • Data Freshness/Health   │
          └────────────────────────┬─────────────────────────┘
                                   │
                                   ▼
                         Local SQLite (health.db)
```

### Metrics Supported Out of the Box

| Metric | Category | Standard Unit | Notes |
| :--- | :--- | :--- | :--- |
| 👟 **Steps** | Activity | count | Summed per day with multi-source deduplication |
| 😴 **Sleep** | Recovery | hours | Provenance-checked interval handling |
| ❤️ **Resting Heart Rate** | Cardiovascular | bpm | Daily baseline tracking & anomaly detection |
| 📈 **HRV** (Heart Rate Variability) | Recovery | ms | RMSSD/SDNN normalized |
| ⚖️ **Weight & Body Fat** | Measurements | kg / % | High-precision episodic measurement tracking |
| 🏋️ **Workouts** | Activity | minutes / kcal | Individual workout sessions with sport metadata |
| 🙂 **Mood** | Wellness | 1-5 scale | Subjective well-being logs |
| 💧 **Water Intake** | Nutrition | ml | Daily fluid tracking |

*The schema is fully extensible with custom metric definitions in `metric_registry.py`.*

---

## 🔌 Client Compatibility

| Client / Assistant | Compatibility | Documentation |
| :--- | :---: | :--- |
| **Claude Desktop** | ✅ Official Support | [Claude Desktop Setup Guide](docs/clients/claude-desktop.md) |
| **Cursor** | ✅ Full Support | [Cursor IDE Setup Guide](docs/clients/cursor.md) |
| **Windsurf** | ✅ Full Support | Configure via `mcp_config.json` |
| **LM Studio** | ✅ Local AI | [LM Studio Setup Guide](docs/clients/lm-studio.md) |
| **Open WebUI** | ✅ Self-Hosted | [Open WebUI Setup Guide](docs/clients/open-webui.md) |
| **Android Health Connect** | ✅ Export & Ingest | [Health Connect Guide](docs/clients/android-health-connect.md) |

---

## 🛠️ Operations & Data Safety

### Atomic WAL Backups
Never copy `health.db` while your server or MCP client is running: SQLite runs in WAL mode, meaning uncommitted writes live in `health.db-wal`. 

Use the built-in backup CLI, which takes a live, consistent snapshot:

```bash
# Create a verified snapshot:
quantified-self-backup create ~/backups/health-2026-11-04.db

# Safe restore (checks schema integrity before moving old DB aside):
quantified-self-backup restore ~/backups/health-2026-11-04.db
```

### Diagnostics
Run anytime to check your Python runtime, database version, and environment configuration:

```bash
quantified-self-doctor
```

---

## 📚 Documentation

Deep-dive documentation and technical specs:

* **[Complete Tool Catalog (20 Tools)](docs/tools.md)**
* **[Data Import Guide (Apple Health, WHOOP, CSV)](docs/importing.md)**
* **[Client Setup Guides](docs/clients/)**
* **[Security & Threat Model](SECURITY.md)**
* **[Contribution Guidelines](CONTRIBUTING.md)**
* **[Changelog](CHANGELOG.md)**

---

## 🤝 Contributing

Contributions are warmly welcomed! We are actively looking for contributions in:
- New wearable & app adapters (Garmin, Oura, Fitbit)
- Specialized analytical & correlation tools
- Evidence engine calibration
- New MCP client integrations

Please review [`CONTRIBUTING.md`](CONTRIBUTING.md) and [`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md) before opening a pull request.

---

## 📄 License

Licensed under the [MIT License](LICENSE).
