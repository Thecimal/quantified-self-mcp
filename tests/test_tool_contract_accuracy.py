"""
The published tool contracts must describe the tools that are really registered.

docs/tools.md used to document parameters (`window_days`, `threshold_std`, `target_date`, `metrics`, `filename`),
defaults and capabilities the tools do not have, so a call written from it failed validation, and
aggregate_measurements pointed clients at a "method" field get_baseline does not return. These tests compare every
documented signature and default with the live tool schemas, and check that the one piece of aggregation semantics a
description states (how each metric rolls up into a day) is the registry's, and is what the server really does.
"""

import ast
import re
import sys
from datetime import date
from pathlib import Path

import pytest
from fastmcp import Client

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from eval.agent.runner import loaded_server  # noqa: E402
from metric_registry import METRICS  # noqa: E402

DOCS = (Path(__file__).resolve().parent.parent / "docs" / "tools.md").read_text(encoding="utf-8")
SIGNATURE = re.compile(r"^\* \*\*`(\w+)\(([^)]*)\)`\*\*", re.MULTILINE)


def _documented() -> dict[str, list[tuple[str, bool, object]]]:
    """tool name -> [(parameter, has_default, default)] in the order docs/tools.md lists them."""
    found: dict[str, list[tuple[str, bool, object]]] = {}
    for match in SIGNATURE.finditer(DOCS):
        params = []
        for part in (p.strip() for p in match.group(2).split(",") if p.strip()):
            name, _, raw = part.partition("=")
            params.append((name.strip(), bool(raw), ast.literal_eval(raw) if raw else None))
        found[match.group(1)] = params
    return found


async def _tools(tmp_path):
    with loaded_server(tmp_path / "health.db", "") as server:
        async with Client(server.mcp) as client:
            return {tool.name: tool for tool in await client.list_tools()}


async def test_every_registered_tool_is_documented_with_its_real_parameters_and_defaults(tmp_path):
    tools = await _tools(tmp_path)
    documented = _documented()
    assert set(documented) == set(tools), (
        f"undocumented: {sorted(set(tools) - set(documented))}; not registered: {sorted(set(documented) - set(tools))}"
    )
    for name, tool in tools.items():
        properties = tool.input_schema.get("properties", {})
        required = set(tool.input_schema.get("required", []))
        expected = [(p, p not in required, properties[p].get("default")) for p in properties]
        actual = [(p, has_default, default) for p, has_default, default in documented[name]]
        assert actual == expected, f"docs/tools.md lists {name}{actual}, the tool takes {expected}"


async def test_the_documented_tool_count_is_the_registered_one(tmp_path):
    tools = await _tools(tmp_path)
    claimed = re.search(r"exposes \*\*(\d+) tools\*\*", DOCS)
    assert claimed and int(claimed.group(1)) == len(tools)


def test_the_docs_do_not_present_explain_metric_change_as_a_causal_explanation():
    # The tool returns facts for the caller to interpret; a bullet claiming causes would overstate it.
    entry = DOCS[DOCS.index("**`explain_metric_change(") :].split("\n\n")[0]
    assert "not a" in entry and "causal explanation" in entry
    assert "causal evidence" not in entry


def _join(names):
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def _rollup_sentence():
    by_method: dict[str, list[str]] = {"sum": [], "mean": [], "last": []}
    for key, definition in METRICS.items():
        by_method[definition.aggregation].append(key)
    return (
        f"summed for {_join(by_method['sum'])}; averaged for {_join(by_method['mean'])}; "
        f"the latest reading for {_join(by_method['last'])}"
    )


async def test_aggregate_measurements_states_the_registry_rollup_and_names_no_missing_field(tmp_path):
    tools = await _tools(tmp_path)
    description = " ".join(tools["aggregate_measurements"].description.split())
    assert _rollup_sentence() in description
    # get_baseline returns no "method" field, so nothing may send a client looking for one.
    assert 'get_baseline\'s "method"' not in description
    assert "method" not in tools["get_baseline"].output_schema["properties"]


async def test_the_stated_rollup_is_what_the_server_does_with_several_readings_in_a_day(tmp_path):
    day = date(2026, 6, 1).isoformat()
    readings = {
        "steps": (1000, 2000),
        "sleep_hours": (3.5, 4.0),
        "resting_heart_rate": (60, 70),
        "weight_kg": (70.0, 71.0),  # the later one is the latest reading
        "workout_minutes": (20, 25),
        "mood": (4, 8),
        "water_ml": (250, 300),
        "heart_rate": (70, 80),
        "hrv_ms": (40.0, 60.0),
    }
    assert set(readings) == set(METRICS)
    expected = {
        metric: {"sum": a + b, "mean": (a + b) / 2, "last": b}[METRICS[metric].aggregation]
        for metric, (a, b) in readings.items()
    }
    with loaded_server(tmp_path / "health.db", "") as server:
        async with Client(server.mcp) as client:
            for metric, (first, second) in readings.items():
                await client.call_tool(
                    "log_measurement", {"timestamp": f"{day}T08:00:00", "metric": metric, "value": first}
                )
                await client.call_tool(
                    "log_measurement", {"timestamp": f"{day}T20:00:00", "metric": metric, "value": second}
                )
            row = (await client.call_tool("read_health_data", {"start_date": day, "end_date": day})).structured_content[
                "rows"
            ][0]
    assert {metric: row[metric] for metric in readings} == pytest.approx(expected)
