"""
Deterministic, offline regression test for the serialized MCP tool-listing
surface (see scripts/measure_tool_surface.py for the measurement method and
why it uses a chars/4 approximation rather than a real tokenizer: a real
tokenizer's vocabulary has to be fetched over the network, which would make
this test flaky/non-reproducible in an offline CI run).

This does not re-litigate what belongs in a tool's description (test_server.py
and test_tool_contract_accuracy.py own that); it only makes sure the total
serialized size doesn't silently creep back up as tools are added or edited.
"""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from scripts.measure_tool_surface import measure  # noqa: E402

# Measured on this repo after the P1 tool-description trim (see
# docs/ or the PR description for the full before/after table). Set with
# ~10% headroom above the as-trimmed total so ordinary, incremental
# description edits don't trip this test, while a real regression
# (e.g. a verbose example block creeping back into every tool, or a new
# tool registered with an untrimmed docstring) will.
MAX_TOTAL_APPROX_TOKENS = 33000
MAX_DESCRIPTION_CHARS_TOTAL = 24000  # sum of every tool's top-level description
EXPECTED_TOOL_COUNT = 20


def test_tool_count_unchanged():
    result = measure()
    assert result["tool_count"] == EXPECTED_TOOL_COUNT


def test_total_serialized_surface_stays_under_budget():
    result = measure()
    assert result["total_approx_tokens"] <= MAX_TOTAL_APPROX_TOKENS, (
        f"serialized tool-listing surface grew to ~{result['total_approx_tokens']:.0f} approx tokens "
        f"(budget {MAX_TOTAL_APPROX_TOKENS}); see scripts/measure_tool_surface.py for a per-tool breakdown"
    )


def test_total_description_text_stays_under_budget():
    """The top-level description (what Args/Returns are stripped down to by
    FastMCP's docstring parsing -- see tests/test_server.py) is the part of
    the tool surface that's actually prose, and the part this P1 task was
    scoped to trim. Catch it creeping back up separately from the (mostly
    fixed, schema-driven) input/output schema bytes."""
    result = measure()
    total_desc_chars = sum(t["description_chars"] for t in result["tools"])
    assert total_desc_chars <= MAX_DESCRIPTION_CHARS_TOTAL, (
        f"total tool description text grew to {total_desc_chars} chars (budget {MAX_DESCRIPTION_CHARS_TOTAL})"
    )


def test_no_single_tool_description_balloons():
    """A per-tool ceiling so one tool's docstring regressing (a pasted
    example block, a restored Returns-style essay moved above Args) doesn't
    get averaged away by the other 19 staying small."""
    result = measure()
    for row in result["tools"]:
        assert row["description_chars"] <= 2600, (
            f"{row['name']}'s description is {row['description_chars']} chars, "
            "well above every other tool's -- check it hasn't regressed"
        )
