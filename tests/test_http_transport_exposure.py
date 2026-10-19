"""The HTTP transport (selected by setting PORT) has no authentication, so it must not listen on every
network interface unless the operator explicitly asks for that with FASTMCP_HOST.

Before the fix server.main() hard-coded host="0.0.0.0": anyone who could reach the machine could call every
tool (read, write, clear, export) with no credentials.
"""

from __future__ import annotations

import logging
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

import server

REPO = Path(__file__).resolve().parent.parent


class _RecordingMcp:
    def __init__(self):
        self.calls = []

    def run(self, *args, **kwargs):
        self.calls.append((args, kwargs))


@pytest.fixture
def recorder(monkeypatch):
    rec = _RecordingMcp()
    monkeypatch.setattr(server, "mcp", rec)
    return rec


def test_without_port_the_server_runs_over_stdio(recorder, monkeypatch):
    monkeypatch.delenv("PORT", raising=False)
    server.main()
    assert recorder.calls == [((), {})]


def test_http_transport_never_hard_codes_a_public_bind(recorder, monkeypatch):
    monkeypatch.setenv("PORT", "8123")
    server.main()
    ((_, kwargs),) = recorder.calls
    assert kwargs["transport"] == "http"
    assert kwargs["port"] == 8123
    # host is left to FastMCP's own setting (127.0.0.1 unless FASTMCP_HOST is set).
    assert kwargs.get("host") in (None, "127.0.0.1", "localhost", "::1")


def test_loopback_http_does_not_warn(recorder, monkeypatch, caplog):
    monkeypatch.setenv("PORT", "8123")
    monkeypatch.setattr(server.fastmcp_settings, "host", "127.0.0.1")
    with caplog.at_level(logging.WARNING, logger="quantified-self-mcp"):
        server.main()
    assert not [r for r in caplog.records if "authentication" in r.getMessage()]


def test_explicit_public_host_is_allowed_but_warns(recorder, monkeypatch, caplog):
    monkeypatch.setenv("PORT", "8123")
    monkeypatch.setattr(server.fastmcp_settings, "host", "0.0.0.0")
    with caplog.at_level(logging.WARNING, logger="quantified-self-mcp"):
        server.main()
    warnings = [r.getMessage() for r in caplog.records if "authentication" in r.getMessage()]
    assert len(warnings) == 1
    assert "0.0.0.0" in warnings[0] and "FASTMCP_HOST" in warnings[0]
    assert len(recorder.calls) == 1  # still starts: the operator asked for it


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _non_loopback_address() -> str | None:
    # A UDP connect() sends no packet; it only asks the OS which local address would be used.
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))
            addr = s.getsockname()[0]
    except OSError:
        return None
    return None if addr.startswith("127.") else addr


def test_real_server_with_port_is_reachable_on_loopback_only(tmp_path):
    other = _non_loopback_address()
    if other is None:
        pytest.skip("no non-loopback IPv4 address available to probe")
    port = _free_port()
    env = {k: v for k, v in os.environ.items() if k not in {"FASTMCP_HOST", "PYTHONPATH"}}
    env.update(PORT=str(port), HEALTH_DB_PATH=str(tmp_path / "health.db"), FASTMCP_CHECK_FOR_UPDATES="off")
    proc = subprocess.Popen(
        [sys.executable, str(REPO / "server.py")],
        cwd=REPO,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 60
        while True:
            assert proc.poll() is None, "server exited before listening"
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=1):
                    break
            except OSError:
                assert time.monotonic() < deadline, "server never started listening on loopback"
                time.sleep(0.25)

        # Plain urllib, not fastmcp's HTTP client: importing httpx into the test process would break
        # tests/test_jev_contract.py's "no HTTP library was imported" check.
        initialize = (
            b'{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26", '
            b'"capabilities": {}, "clientInfo": {"name": "exposure-test", "version": "0"}}}'
        )
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/mcp",
            data=initialize,
            headers={"Content-Type": "application/json", "Accept": "application/json, text/event-stream"},
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            assert response.status == 200
            assert b"Quantified Self" in response.read()

        with pytest.raises(OSError), socket.create_connection((other, port), timeout=2):
            pass
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
