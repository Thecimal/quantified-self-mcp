# LM Studio

Tested with LM Studio's built-in MCP support (Program tab → `mcp.json`), running a local model.

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

In LM Studio, open the **Program** tab (right sidebar) → **Install → Edit mcp.json**, and add:

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

Windows: use `C:\\absolute\\path\\to\\quantified-self-mcp\\.venv\\Scripts\\python.exe` for `command`.

Load any local model that supports tool calling (e.g. a Qwen or Llama instruct model) in the **Chat** tab first.

## 4. Restart LM Studio

Reload the chat session (or restart the app) so it picks up the new `mcp.json` entry. `quantified-self` should appear under the tools/plug icon in the chat sidebar.

## 5. Ask this exact question

```
How has my sleep changed over the last 30 days?
```

## 6. Expected result

The model issues a `read_health_data` tool call, gets back the sample rows plus computed averages, and answers using those numbers. Smaller local models sometimes need a nudge like "use the quantified-self tool" the first time.

## Troubleshooting

- **Tool not listed**: check `mcp.json` is valid JSON and the `command` path exists; restart LM Studio fully.
- **Model never calls the tool**: confirm the loaded model supports tool/function calling — not all local models do.
- **"No health database found"**: re-run step 2.
