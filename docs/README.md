# Documentation

Comprehensive guides and technical references for Quantified Self MCP.

## Guides

* **[Importing Health Data](importing.md)**: Importing CSVs, Apple Health XML exports, and Android Health Connect JSON records.
* **[Tool Reference](tools.md)**: Full catalog of all 20 MCP tools and resources across data access, measurements, workouts, and analytics.

## MCP Client Setup

Step-by-step connection guides for MCP clients:

* **[Claude Desktop](clients/claude-desktop.md)**: Configuration for macOS, Windows, and Linux.
* **[LM Studio](clients/lm-studio.md)**: Local LLM tool use via `mcp.json`.
* **[Open WebUI](clients/open-webui.md)**: WebUI integration via `mcpo` proxy.
* **[Android Health Connect](clients/android-health-connect.md)**: Setting up Health Connect JSON exports.

## Diagnostics

Verify your environment and database setup at any time with the diagnostic tool:

```bash
quantified-self-doctor
# or
python doctor.py
```
