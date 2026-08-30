# Quantified Self MCP

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

The server currently provides:

**`read_health_data`**

It can access:

* Daily steps
* Sleep duration
* Resting heart rate
* Data for a selected date range

Access through the MCP tool is **read-only**.

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
