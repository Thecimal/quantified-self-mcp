from __future__ import annotations

import subprocess
import sys
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


@pytest.mark.parametrize("flag", ["--help", "-h", "help"])
def test_help_flags_exit_zero_and_never_call_mcp_run(recorder, flag, capsys):
    with pytest.raises(SystemExit) as excinfo:
        server.main([flag])

    assert excinfo.value.code == 0
    assert recorder.calls == []
    captured = capsys.readouterr()
    assert "usage:" in captured.out or "usage:" in captured.err
    assert "quantified-self-mcp" in captured.out or "quantified-self-mcp" in captured.err


@pytest.mark.parametrize("flag", ["--version", "-v", "-V", "version"])
def test_version_flags_exit_zero_and_never_call_mcp_run(recorder, flag, capsys):
    with pytest.raises(SystemExit) as excinfo:
        server.main([flag])

    assert excinfo.value.code == 0
    assert recorder.calls == []
    captured = capsys.readouterr()
    output = captured.out or captured.err
    assert server.__version__ in output
    assert "quantified-self-mcp" in output


def test_invalid_cli_flag_exits_code_2_and_never_call_mcp_run(recorder, capsys):
    with pytest.raises(SystemExit) as excinfo:
        server.main(["--nonexistent-flag"])

    assert excinfo.value.code == 2
    assert recorder.calls == []


def test_cli_explicit_transport_and_port(recorder):
    server.main(["--transport", "http", "--port", "9999"])
    assert len(recorder.calls) == 1
    args, kwargs = recorder.calls[0]
    assert kwargs.get("transport") == "http"
    assert kwargs.get("port") == 9999


def test_cli_http_transport_without_port_errors(recorder):
    with pytest.raises(SystemExit) as excinfo:
        server.main(["--transport", "http"])
    assert excinfo.value.code != 0
    assert recorder.calls == []


def test_subprocess_help_returns_immediately():
    """Verify that python server.py --help and quantified-self-mcp --help never block."""
    result = subprocess.run(
        [sys.executable, "server.py", "--help"],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 0
    assert "usage: quantified-self-mcp" in result.stdout

    # Test entrypoint script if installed in the environment
    entrypoint = Path(sys.prefix) / "bin" / "quantified-self-mcp"
    if entrypoint.exists():
        ep_result = subprocess.run(
            [str(entrypoint), "--help"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        assert ep_result.returncode == 0
        assert "usage: quantified-self-mcp" in ep_result.stdout


def test_subprocess_version_returns_immediately():
    """Verify that quantified-self-mcp --version invoked via CLI never blocks."""
    result = subprocess.run(
        [sys.executable, "server.py", "--version"],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 0
    assert f"quantified-self-mcp {server.__version__}" in result.stdout
