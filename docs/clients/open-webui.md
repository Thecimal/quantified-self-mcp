# Open WebUI

Open WebUI calls tools over OpenAPI, not raw MCP stdio, so this server is bridged through [`mcpo`](https://github.com/open-webui/mcpo), the official MCP-to-OpenAPI proxy. Tested with Open WebUI + Ollama.

## 1. Install

```bash
git clone https://github.com/Thecimal/quantified-self-mcp.git
cd quantified-self-mcp
python3 -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
pip install mcpo
```

## 2. Import sample data

```bash
python init_db.py sample_data/health_sample.csv
```

## 3. Copy this config

Start the proxy, pointing it at the server through the same venv interpreter:

```bash
mcpo --port 8765 -- /absolute/path/to/quantified-self-mcp/.venv/bin/python3 /absolute/path/to/quantified-self-mcp/server.py
```

In Open WebUI, go to **Settings → Tools → Add Tool Server** and add:

```
http://localhost:8765
```

## 4. Restart / reconnect

Open WebUI validates the OpenAPI schema on save — you should see `quantified-self` tools (`read_health_data`, `read_finance_data`) listed immediately, no app restart needed. Keep the `mcpo` process running in the background.

## 5. Ask this exact question

In a chat with tools enabled for the model:

```
How has my sleep changed over the last 30 days?
```

## 6. Expected result

Open WebUI shows a tool-call step for `read_health_data`, then the model answers using the returned averages for sleep, steps, and resting heart rate.

## Troubleshooting

- **Tool server won't add**: confirm `mcpo` is still running and `http://localhost:8765` is reachable in a browser (it serves an OpenAPI docs page).
- **No tool-call step appears**: confirm the selected model in Open WebUI has tool calling enabled in its model settings.
- **"No health database found"**: re-run step 2.
