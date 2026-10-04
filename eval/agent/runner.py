"""Runs scenarios: builds the fixture, loads the real server on it, drives the agent loop, scores the trace."""

from __future__ import annotations

import importlib
import os
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from fastmcp import Client

from eval.agent.fixtures import FIXTURES, REPO_ROOT, build_fixture
from eval.agent.loop import Trace, run_agent
from eval.agent.model import ModelClient
from eval.agent.scenarios import Scenario, resolve
from eval.agent.scoring import Score, score_trace

SERVER_MODULES = ("server", "privacy", "tools.health", "tools.measurements", "tools.workouts", "tools.status")
SYSTEM_TEMPLATE = (
    "You are the assistant embedded in a personal quantified-self app. The user's health data is only "
    "available through the tools provided. Call whichever tools the question needs (one is often enough), "
    "then answer the user. Today's date is {today}."
)


@dataclass
class ScenarioResult:
    scenario: Scenario
    trace: Trace
    score: Score


@contextmanager
def loaded_server(db_path: Path, private_fields: str = "") -> Iterator[Any]:
    """Import the real server module bound to db_path, restoring the environment afterwards."""
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    saved = {key: os.environ.get(key) for key in ("HEALTH_DB_PATH", "HEALTH_PRIVATE_FIELDS")}
    os.environ["HEALTH_DB_PATH"] = str(db_path)
    os.environ["HEALTH_PRIVATE_FIELDS"] = private_fields
    for name in SERVER_MODULES:
        sys.modules.pop(name, None)
    try:
        yield importlib.import_module("server")
    finally:
        for name in SERVER_MODULES:
            sys.modules.pop(name, None)
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


async def live_tool_info() -> dict[str, bool]:
    """name -> readOnlyHint for every tool the server registers right now."""
    with tempfile.TemporaryDirectory() as tmp:
        with loaded_server(Path(tmp) / "health.db") as server:
            async with Client(server.mcp) as client:
                tools = await client.list_tools()
    return {t.name: getattr(t.annotations, "read_only_hint", None) is True for t in tools}


async def run_scenario(
    scenario: Scenario, model: ModelClient, work_dir: Path, today: date, readonly: dict[str, bool] | None = None
) -> ScenarioResult:
    fixture = FIXTURES[scenario.fixture]
    db_path = work_dir / f"{scenario.id}.db"
    build_fixture(fixture, db_path, today)
    with loaded_server(db_path, fixture.private_fields) as server:
        async with Client(server.mcp) as client:
            trace = await run_agent(
                model,
                client,
                scenario_id=scenario.id,
                question=resolve(scenario.question, today),
                system=SYSTEM_TEMPLATE.format(today=today.isoformat()),
            )
            if readonly is None:
                readonly = {
                    t.name: getattr(t.annotations, "read_only_hint", None) is True for t in await client.list_tools()
                }
    return ScenarioResult(scenario, trace, score_trace(scenario, trace, readonly, today))


async def run_all(
    scenarios: list[Scenario], model: ModelClient, work_dir: Path, today: date, progress=print
) -> list[ScenarioResult]:
    readonly = await live_tool_info()
    results = []
    for scenario in scenarios:
        result = await run_scenario(scenario, model, work_dir, today, readonly)
        results.append(result)
        verdict = "PASS" if result.score.passed else "FAIL"
        progress(f"[{verdict}] {scenario.id}: {result.trace.called_tools() or '(no tools)'}")
    return results
