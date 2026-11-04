# Cursor

Tested with Cursor IDE on macOS, Linux, and Windows.

## 1. Prerequisites

Ensure you have Python installed, or `uv` / `uvx` available on your system.

Option A: Run directly with `uvx` (no local clone or manual install needed):
```bash
# uvx handles package installation automatically in an isolated environment
uvx quantified-self-mcp
```

Option B: Install via `pip`:
```bash
pip install quantified-self-mcp
```

## 2. Configure MCP in Cursor

You can configure Quantified Self MCP in Cursor either through the UI or via `.cursor/mcp.json`.

### Method A: Via Cursor Settings UI

1. Open **Cursor Settings** (`Cmd + ,` on macOS, `Ctrl + ,` on Windows/Linux).
2. Navigate to **Features → MCP Servers**.
3. Click **+ Add New MCP Server**.
4. Fill in the details:
   - **Name**: `quantified-self`
   - **Type**: `command` (stdio)
   - **Command**: `uvx quantified-self-mcp` (or the path to your installed `quantified-self-mcp` executable)

### Method B: Via `.cursor/mcp.json`

Create or edit `.cursor/mcp.json` in your workspace or home directory:

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

If using a virtual environment or pip-installed binary:

```json
{
  "mcpServers": {
    "quantified-self": {
      "command": "quantified-self-mcp"
    }
  }
}
```

## 3. Verify Connection

1. Open Cursor's Chat panel (`Cmd + L` or `Ctrl + L`).
2. Type `/` or check the Composer / Chat tool drawer to verify `quantified-self` tools (e.g., `read_health_data`, `calculate_metric_trend`, `explain_metric_change`) are active.
3. Ask Cursor:
   ```text
   What was my average sleep over the last 30 days?
   ```
