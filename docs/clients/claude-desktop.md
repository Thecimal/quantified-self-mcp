# Claude Desktop

Tested with Claude Desktop on macOS, Windows, and Linux.

---

## Option 1: Run with `uvx` (Recommended — No clone needed)

If you have `uv` installed, Claude Desktop can run Quantified Self MCP directly without managing virtual environments:

Open your Claude Desktop configuration file:
- **macOS**: `~/Library/Application Support/Claude/claude_desktop_config.json`
- **Windows**: `%APPDATA%\Claude\claude_desktop_config.json`
- **Linux**: `~/.config/Claude/claude_desktop_config.json`

Or in the app: **Settings → Developer → Edit Config**.

Add this entry under `mcpServers`:

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

---

## Option 2: Local Source / Virtual Environment

If you are developing locally or prefer a fixed virtual environment:

### 1. Clone & Install

```bash
git clone https://github.com/Thecimal/quantified-self-mcp.git
cd quantified-self-mcp
python3 -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -e .
```

### 2. Import Data

```bash
quantified-self-init-db sample_data/health_sample.csv
```

### 3. Configure Claude Desktop

Add this entry under `mcpServers` in `claude_desktop_config.json`:

**macOS / Linux**:
```json
{
  "mcpServers": {
    "quantified-self": {
      "command": "/absolute/path/to/quantified-self-mcp/.venv/bin/quantified-self-mcp"
    }
  }
}
```

**Windows**:
```json
{
  "mcpServers": {
    "quantified-self": {
      "command": "C:\\absolute\\path\\to\\quantified-self-mcp\\.venv\\Scripts\\quantified-self-mcp.exe"
    }
  }
}
```

---

## 4. Restart Claude Desktop

Fully quit and reopen Claude Desktop (closing the window is not enough). Look for the hammer/tools icon in the chat input — `quantified-self` tools should be active.

Ask Claude:
```text
How has my sleep and resting heart rate changed over the last 30 days?
```
