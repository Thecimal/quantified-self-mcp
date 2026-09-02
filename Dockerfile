# Dockerfile for quantified-self-mcp (https://github.com/Thecimal/quantified-self-mcp)
#
# This server is Python-only (FastMCP) — there is no server.js / Node.js
# entrypoint anywhere in this repo. Building from an explicit Python base
# image here, instead of relying on Glama's generic buildpack, guarantees
# `python` and `pip` are actually present in the image and that the
# platform launches `python server.py` (not a Node.js fallback).
#
# Build:
#   docker build -t quantified-self-mcp .
# Run (stdio MCP server):
#   docker run -i --rm -v "$PWD/data:/data" quantified-self-mcp

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Install dependencies first so this layer is cached across source edits.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Application source. init_db.py and sample_data are included so the
# server can be seeded with example data inside the container.
COPY server.py init_db.py logic.py ./
COPY sample_data ./sample_data

# health.db lives here by default (see server.py). Point it at Glama's
# persistent volume mount (/data) so a redeploy doesn't wipe your data;
# override with your own path if you're not on Glama.
ENV HEALTH_DB_PATH=/data/health.db

# Talks to its client over stdio — Glama (and Claude Desktop) wrap stdio
# servers automatically, so the container just needs to run the process.
ENTRYPOINT ["python", "server.py"]
