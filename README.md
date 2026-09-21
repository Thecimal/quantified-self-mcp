
# Quantified Self MCP
[![CI](https://github.com/Thecimal/quantified-self-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/Thecimal/quantified-self-mcp/actions)
[![PyPI](https://img.shields.io/pypi/v/quantified-self-mcp.svg)](https://pypi.org/project/quantified-self-mcp/)
[![License](https://img.shields.io/github/license/Thecimal/quantified-self-mcp)](https://github.com/Thecimal/quantified-self-mcp/blob/main/LICENSE)
[![M8ven Score](https://m8ven.ai/badge/mcp/thecimal-quantified-self-mcp-v6tlvp)](https://m8ven.ai/mcp/thecimal-quantified-self-mcp-v6tlvp)
[![Quantified Self MCP Server MCP server – quality and maintenance score on Glama](https://glama.ai/mcp/servers/Thecimal/quantified-self-mcp/badges/score.svg)](https://glama.ai/mcp/servers/Thecimal/quantified-self-mcp)

<img width="1771" height="608" alt="Gemini_Generated_Image_uohcyiuohcyiuohc" src="https://github.com/user-attachments/assets/79e7d666-bf04-41bd-9399-4c3bf3254983" />


> **Your health data. Your AI. Your machine.**
>
> A local-first MCP server that gives AI agents access to your personal health data — with **privacy, provenance, and evidence built in**.


· **[Documentation](docs/)**

---

## What is it?

Quantified Self MCP gives an AI agent a **local, structured interface to your own health history**.

Instead of another health dashboard, you can ask your AI questions like:

```text
Why did my HRV change recently?

How has my sleep changed over the last 30 days?

What happened to my resting heart rate after my workouts increased?

What patterns do you see across my recent health data?

Which metrics changed the most this month?
```

The AI retrieves the relevant data through MCP and analyzes it.

> **The goal isn't another health dashboard.**
>
> **It's a trustworthy interface between your health history and your AI.**

---

## Why is it different?

### 🔒 Local-first

Your health database runs locally in SQLite.

You control where your data goes.

You can use a completely local AI stack:

```text
Health Data
     ↓
Local SQLite
     ↓
Quantified Self MCP
     ↓
MCP Agent
     ↓
Local LLM
```

Cloud models are also supported. In that configuration, the MCP database remains local, but data returned by MCP tools may be sent to the model provider.

### 🤖 AI-native

Built specifically for **MCP-compatible AI agents**, rather than another standalone health application.

### 🔎 Evidence & provenance

Analytics can be traced back to the underlying health data and its provenance.

The goal is not simply:

```text
HRV ↓ 24%
```

but understanding:

```text
What data produced this result?
Where did it come from?
What period was analyzed?
How strong is the evidence?
```

Concretely, every trend, baseline, anomaly, comparison, and correlation carries a coverage/confidence object alongside its numbers, so the AI can talk about the *result* and the *evidence behind it* in the same breath:

**Without evidence:**
> Your HRV decreased 24% over the last 30 days.

**With evidence:**
> Your HRV decreased 24% over the last 30 days — but HRV was only logged on 71% of those days, so treat this trend cautiously rather than as a settled pattern.

The second answer is what `calculate_metric_trend` (and every other analytics tool) is designed to make possible: the tool returns the 24% figure *and* a `confidence: "moderate"` / `coverage_ratio: 0.71` alongside it, and each tool's own description tells the calling model to fold that into its answer instead of reporting the number as if it came from a complete series.

### 📊 Longitudinal

Analyze health history across days, weeks, months, and years:

- trends
- baselines
- anomalies
- period comparisons
- correlations
- recent changes
- metric explanations

### 📥 Import existing data

Bring your existing health history into the local database.

Supported imports include:

- CSV
- Apple Health exports

See the [import documentation](docs/).

---

## What can your AI do?

### Read

- Health metrics
- Metric history
- Raw measurements
- Workout sessions
- Data provenance

### Analyze

- Baselines
- Trends
- Anomalies
- Period comparisons
- Correlations

### Explain

- Recent changes
- Metric changes
- Supporting evidence
- Data provenance

The MCP currently exposes **18 tools** across data access, measurements, workouts, analytics, and personal intelligence.

See the [tool reference](docs/) for the complete list.

---

## Supported data

Currently supported metrics include:

- 👟 Steps
- 😴 Sleep
- ❤️ Heart rate
- ❤️ Resting heart rate
- 📈 HRV
- ⚖️ Weight
- 🏋️ Workout minutes
- 🙂 Mood
- 💧 Water

The data model is extensible, so you can keep only the metrics you actually use.

---

## Quick start

### Install

```bash
pip install quantified-self-mcp
```

### Import your data

CSV:

```bash
quantified-self-init-db your-health-data.csv
```

Apple Health:

```bash
quantified-self-init-db export.xml
```

### Connect an MCP client

Configure your preferred MCP-compatible client to run:

```bash
quantified-self-mcp
```

Client-specific setup guides are available in [`docs/clients/`](docs/clients/).

### Ask your AI

```text
How has my sleep changed over the last 30 days?
```

That's it.

---

## Privacy

Health data is sensitive.

Quantified Self MCP is designed around **local ownership**:

- Your database stays on your machine.
- No proprietary health-data cloud is required.
- You choose the AI model.
- You can run the entire stack locally.
- Private metrics can be excluded from AI access.

For example:

```bash
HEALTH_PRIVATE_FIELDS=weight_kg,mood
```

Private fields remain stored locally but are excluded from MCP read and analytical operations.

See [`SECURITY.md`](SECURITY.md) for security considerations.

---

## Architecture

```text
                  AI Agent
                     │
                 MCP Protocol
                     │
                     ▼
          ┌─────────────────────┐
          │ Quantified Self MCP │
          │                     │
          │  Data · Analytics   │
          │  Evidence           │
          └──────────┬──────────┘
                     │
                     ▼
              Local SQLite
                     │
                     ▼
              Your Health Data
```

The MCP server is the bridge between your health history and your AI.

---

## Documentation

Detailed documentation lives outside the README:

- **[Client setup](docs/clients/)**
- **[Importing health data](docs/)**
- **[Tool reference](docs/)**
- **[Security](SECURITY.md)**
- **[Contributing](CONTRIBUTING.md)**
- **[Changelog](CHANGELOG.md)**

---

## Open source

Quantified Self MCP is open source and built for the wider MCP ecosystem.

Contributions are welcome — especially around:

- health-data imports
- analytics
- evidence and data quality
- privacy
- MCP client integrations
- documentation

See [`CONTRIBUTING.md`](CONTRIBUTING.md).

---

## License

MIT

---

> **Your health data. Your AI. Your machine.**
>
> **Local data. Open protocol. AI-powered insight.**
