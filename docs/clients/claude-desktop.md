# Claude Desktop

Tested with Claude Desktop on macOS and Windows.

## 1. Install

```bash
git clone https://github.com/Thecimal/quantified-self-mcp.git
cd quantified-self-mcp
python3 -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

## 2. Import sample data

```bash
python init_db.py sample_data/health_sample.csv
```

## 3. Copy this config

Open the config file:

- **macOS**: `~/Library/Application Support/Claude/claude_desktop_config.json`
- **Windows**: `%APPDATA%\Claude\claude_desktop_config.json`
- **Linux**: `~/.config/Claude/claude_desktop_config.json`

Or from the app: **Settings → Developer → Edit Config**.

Add this entry under `mcpServers`, using the **absolute path** to the Python interpreter inside the `.venv` you just created (not a bare `python`):

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

Windows:

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

## 4. Restart Claude Desktop

Fully quit and reopen the app (closing the window is not enough). Look for the hammer/tools icon in the chat box — `quantified-self` should be listed.

## 5. Ask this exact question

```
How has my sleep changed over the last 30 days?
```

## 6. Expected result

Claude calls `read_health_data`, then summarizes average sleep hours, steps, and resting heart rate across the imported sample range, usually noting the trend direction.

## Troubleshooting

- **Server doesn't show up**: confirm `command` points at the `.venv` interpreter (not system Python), that the path exists, and that you fully restarted the app.
- **Logs**: `~/Library/Logs/Claude/mcp-server-quantified-self.log` (macOS) or `%APPDATA%\Claude\logs\mcp-server-quantified-self.log` (Windows).
- **"No health database found"**: re-run step 2 — the tools don't auto-create empty databases.
