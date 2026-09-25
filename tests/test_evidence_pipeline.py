"""End-to-end enforcement of the evidence pipeline at the MCP boundary.

Every analytical tool must return its own evidence_profile and claim_decision, produced by
registry policy -> evaluators -> EvidenceProfile -> weakest-link resolver -> ClaimDecision, and the
decision must change when the evidence is deliberately degraded while the statistical result stays put.

Everything goes through fastmcp.Client (real tool bodies, real SQLite, real protocol layer).

Note on tiers: robustness, practical and provenance have no evaluator yet, so they resolve as
not_assessed and cap every claim at "suggestive". "supported" is unreachable until they exist, which is
why the degradation tests assert on limiting factors (must_state) and on "insufficient", not on "supported".
"""

import sys
from datetime import date, timedelta
from pathlib import Path

import pytest
from fastmcp import Client

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from qs_evidence import load_registry  # noqa: E402
from qs_evidence.models import TIER_RANK, ClaimTier  # noqa: E402

REG = load_registry()
START = date(2026, 3, 1)


def unbuilt(analysis):
    """Factors expected while robustness/practical/provenance have no evaluator: only the dimensions this
    analysis's registry entry says to cap on (measurement_validity and, for correlation, provenance are tolerated)."""
    spec = REG[analysis].dimensions
    return {
        f"{d}_not_assessed"
        for d in ("robustness", "practical", "provenance")
        if d in {k.value for k in spec} and spec[next(k for k in spec if k.value == d)].on_not_assessed == "cap"
    }


@pytest.fixture
def health_db(tmp_path, monkeypatch):
    monkeypatch.setenv("HEALTH_DB_PATH", str(tmp_path / "health.db"))
    sys.modules.pop("server", None)
    import server

    return server


@pytest.fixture
async def client(health_db):
    async with Client(health_db.mcp) as c:
        yield c


async def seed(client, metric, values, start=START):
    """Log one value per day starting at `start`; None leaves that day empty."""
    for i, v in enumerate(values):
        if v is not None:
            await client.call_tool("log_daily_metric", {"date": (start + timedelta(days=i)).isoformat(), metric: v})


def iso(d):
    return d.isoformat()


def gap_last(values, k):
    return values[: len(values) - k] + [None] * k


def rank(sc, key="claim_decision"):
    return TIER_RANK[ClaimTier(sc[key]["tier"])]


def dims(sc, key="evidence_profile"):
    return {d["dimension"]: d for d in sc[key]["dimensions"]}


def assert_pipeline_output(sc, analysis, key_prefix=""):
    """The response carries a real profile + decision for `analysis`, shaped by that analysis's registry entry."""
    profile, decision = sc[f"{key_prefix}evidence_profile"], sc[f"{key_prefix}claim_decision"]
    assert profile is not None and decision is not None
    assert profile["analysis"] == analysis
    assert {d["dimension"] for d in profile["dimensions"]} == {d.value for d in REG[analysis].dimensions}
    assert decision["tier"] in {t.value for t in ClaimTier}
    assert isinstance(decision["must_state"], list) and decision["template"]
    return profile, decision


# ---- trend ----------------------------------------------------------------------------------------------


async def test_trend_same_slope_different_evidence_different_decision(client):
    linear = [5000 + 100 * i for i in range(30)]  # exactly +100/day whatever days are dropped
    args = {"metric": "steps", "start_date": iso(START), "end_date": iso(START + timedelta(days=29))}

    await seed(client, "steps", linear)
    clean = (await client.call_tool("calculate_metric_trend", args)).structured_content
    await client.call_tool("clear_metric", {"date": iso(START + timedelta(days=24)), "field": "steps"})
    for i in range(24, 30):
        await client.call_tool("clear_metric", {"date": iso(START + timedelta(days=i)), "field": "steps"})
    gappy = (await client.call_tool("calculate_metric_trend", args)).structured_content

    assert clean["trend"]["slope_per_day"] == gappy["trend"]["slope_per_day"] == 100.0  # same statistical result
    _, d_clean = assert_pipeline_output(clean, "trend")
    _, d_gappy = assert_pipeline_output(gappy, "trend")
    assert set(d_clean["must_state"]) == unbuilt("trend")  # nothing wrong with the data itself
    assert "recent_window_gap" in d_gappy["must_state"] and "recent_window_gap" not in d_clean["must_state"]
    assert rank(gappy) <= rank(clean)


async def test_trend_tiny_sample_is_insufficient_and_never_ranks_above_better_evidence(client):
    await seed(client, "steps", [5000 + 100 * i for i in range(10)])
    sc = (
        await client.call_tool(
            "calculate_metric_trend",
            {"metric": "steps", "start_date": iso(START), "end_date": iso(START + timedelta(days=29))},
        )
    ).structured_content
    _, decision = assert_pipeline_output(sc, "trend")
    assert decision["tier"] == "insufficient" and "n_below_minimum" in decision["must_state"]
    assert dims(sc)["sample"]["status"] == "blocking"


async def test_trend_claim_envelope_is_an_exact_mirror_of_the_legacy_fields(client):
    """Migration invariant: claim.{evidence,profile,decision} must equal the deprecated flat fields,
    not merely both be present. Prevents the canonical and legacy representations from diverging
    silently during the deprecation window."""
    await seed(client, "steps", [5000 + 100 * i for i in range(30)])
    sc = (
        await client.call_tool(
            "calculate_metric_trend",
            {"metric": "steps", "start_date": iso(START), "end_date": iso(START + timedelta(days=29))},
        )
    ).structured_content
    assert set(sc["claim"]) == {"evidence", "profile", "decision"}
    assert sc["claim"]["evidence"] == sc["evidence"]
    assert sc["claim"]["profile"] == sc["evidence_profile"]
    assert sc["claim"]["decision"] == sc["claim_decision"]


async def test_trend_schema_declares_claim_and_deprecates_legacy_fields(client):
    schema = {t.name: t for t in await client.list_tools()}["calculate_metric_trend"].output_schema
    assert "claim" in schema["properties"] and "claim" in schema["required"]
    for legacy in ("evidence", "evidence_profile", "claim_decision"):
        assert schema["properties"][legacy].get("deprecated") is True
        assert legacy in schema["required"]  # deprecated, not optional, during the migration window


# ---- period / window comparison ---------------------------------------------------------------------------


async def test_compare_periods_same_delta_different_evidence_different_decision(client):
    a_start = START + timedelta(days=14)
    period = {
        "metric": "steps",
        "period_a_start": iso(a_start),
        "period_a_end": iso(a_start + timedelta(days=13)),
        "period_b_start": iso(START),
        "period_b_end": iso(START + timedelta(days=13)),
    }
    await seed(client, "steps", [5000] * 14, START)
    await seed(client, "steps", [9000] * 14, a_start)
    clean = (await client.call_tool("compare_metric_periods", period)).structured_content
    for i in range(8, 14):  # period A loses its last 6 days; mean stays 9000
        await client.call_tool("clear_metric", {"date": iso(a_start + timedelta(days=i)), "field": "steps"})
    gappy = (await client.call_tool("compare_metric_periods", period)).structured_content
    for i in range(5, 8):  # ...and then all but 5 days
        await client.call_tool("clear_metric", {"date": iso(a_start + timedelta(days=i)), "field": "steps"})
    tiny = (await client.call_tool("compare_metric_periods", period)).structured_content

    assert clean["delta"] == gappy["delta"] == tiny["delta"] == 4000  # same statistical result throughout
    _, d_clean = assert_pipeline_output(clean, "window_comparison")
    _, d_gappy = assert_pipeline_output(gappy, "window_comparison")
    _, d_tiny = assert_pipeline_output(tiny, "window_comparison")
    assert set(d_clean["must_state"]) == unbuilt("window_comparison")
    assert "recent_window_gap" in d_gappy["must_state"]
    assert d_tiny["tier"] == "insufficient" and "n_below_minimum" in d_tiny["must_state"]
    assert rank(clean) >= rank(gappy) >= rank(tiny)
    which = dims(gappy)["temporal"]["details"]["windows"]
    assert which["period_a"]["status"] == "weak" and which["period_b"]["status"] == "adequate"


async def test_compare_periods_claim_envelope_is_an_exact_mirror_of_the_legacy_fields(client):
    """Migration invariant: claim.{evidence_a,evidence_b,profile,decision} must equal the deprecated
    flat fields, not merely both be present. Prevents the canonical and legacy representations from
    diverging silently during the deprecation window."""
    a_start = START + timedelta(days=14)
    await seed(client, "steps", [5000] * 14, START)
    await seed(client, "steps", [9000] * 14, a_start)
    sc = (
        await client.call_tool(
            "compare_metric_periods",
            {
                "metric": "steps",
                "period_a_start": iso(a_start),
                "period_a_end": iso(a_start + timedelta(days=13)),
                "period_b_start": iso(START),
                "period_b_end": iso(START + timedelta(days=13)),
            },
        )
    ).structured_content
    assert set(sc["claim"]) == {"evidence_a", "evidence_b", "profile", "decision"}
    assert sc["claim"]["evidence_a"] == sc["period_a_evidence"]
    assert sc["claim"]["evidence_b"] == sc["period_b_evidence"]
    assert sc["claim"]["profile"] == sc["evidence_profile"]
    assert sc["claim"]["decision"] == sc["claim_decision"]


async def test_compare_periods_schema_declares_claim_and_deprecates_legacy_fields(client):
    schema = {t.name: t for t in await client.list_tools()}["compare_metric_periods"].output_schema
    assert "claim" in schema["properties"] and "claim" in schema["required"]
    for legacy in ("period_a_evidence", "period_b_evidence", "evidence_profile", "claim_decision"):
        assert schema["properties"][legacy].get("deprecated") is True
        assert legacy in schema["required"]  # deprecated, not optional, during the migration window


# ---- correlation ------------------------------------------------------------------------------------------


def paired_series(n):
    s = [(i * 7) % 11 for i in range(n)]
    return [5000 + 1000 * v for v in s], [1 + v // 2 for v in s]


async def test_correlation_sample_blocks_but_n_eff_never_does(client):
    steps, mood = paired_series(40)
    await seed(client, "steps", steps)
    await seed(client, "mood", mood)
    args = {
        "metric_a": "steps",
        "metric_b": "mood",
        "start_date": iso(START),
        "end_date": iso(START + timedelta(days=39)),
    }
    ok = (await client.call_tool("find_metric_correlation", args)).structured_content
    _, d_ok = assert_pipeline_output(ok, "correlation")
    assert set(d_ok["must_state"]) == unbuilt("correlation") and ok["n"] == 40
    checks = {c["name"]: c for c in dims(ok)["sample"]["details"]["checks"]}
    assert checks["n_paired"]["observed"] == ok["n"]  # the evaluator judged exactly the pairs the tool used

    short = (
        await client.call_tool("find_metric_correlation", {**args, "end_date": iso(START + timedelta(days=9))})
    ).structured_content
    _, d_short = assert_pipeline_output(short, "correlation")
    assert d_short["tier"] == "insufficient" and "n_paired_below_minimum" in d_short["must_state"]
    assert rank(short) < rank(ok)


async def test_correlation_low_n_eff_alone_stays_out_of_insufficient(client):
    await seed(client, "steps", [5000 + 30 * i for i in range(40)])  # slow ramps: lag-1 autocorrelation ~ 1
    await seed(client, "mood", [1 + i // 5 for i in range(40)])
    sc = (
        await client.call_tool(
            "find_metric_correlation",
            {
                "metric_a": "steps",
                "metric_b": "mood",
                "start_date": iso(START),
                "end_date": iso(START + timedelta(days=39)),
            },
        )
    ).structured_content
    _, decision = assert_pipeline_output(sc, "correlation")
    sample = dims(sc)["sample"]
    assert sample["reason_codes"] == ["n_eff_low"]
    assert sample["status"] == "weak" and sample["can_block"] is False  # diagnostic, cannot block
    assert decision["tier"] != "insufficient" and "n_eff_low" in decision["must_state"]


async def test_correlation_lag_pairs_match_the_tools_own_join(client):
    steps, mood = paired_series(40)
    await seed(client, "steps", steps)
    await seed(client, "mood", mood)
    for lag in (0, 1, 3):
        sc = (
            await client.call_tool(
                "find_metric_correlation",
                {
                    "metric_a": "steps",
                    "metric_b": "mood",
                    "start_date": iso(START),
                    "end_date": iso(START + timedelta(days=39)),
                    "lag_days": lag,
                },
            )
        ).structured_content
        checks = {c["name"]: c for c in dims(sc)["sample"]["details"]["checks"]}
        assert checks["n_paired"]["observed"] == sc["n"], lag


async def test_correlation_has_no_sample_confidence_field(client):
    """Only one confidence surface may reach the model for a correlation: claim.decision. A second,
    unstructured "sample_confidence" bucket would let the model treat the paired-sample size as
    independently authoritative instead of reading it through the resolved decision."""
    steps, mood = paired_series(40)
    await seed(client, "steps", steps)
    await seed(client, "mood", mood)
    sc = (
        await client.call_tool(
            "find_metric_correlation",
            {
                "metric_a": "steps",
                "metric_b": "mood",
                "start_date": iso(START),
                "end_date": iso(START + timedelta(days=39)),
            },
        )
    ).structured_content
    assert "sample_confidence" not in sc
    assert "sample_confidence" not in sc["claim"]


async def test_correlation_claim_envelope_is_an_exact_mirror_of_the_legacy_fields(client):
    """Migration invariant: claim.{evidence_a,evidence_b,profile,decision} must equal the deprecated
    flat fields, not merely both be present. Prevents the canonical and legacy representations from
    diverging silently during the deprecation window."""
    steps, mood = paired_series(40)
    await seed(client, "steps", steps)
    await seed(client, "mood", mood)
    sc = (
        await client.call_tool(
            "find_metric_correlation",
            {
                "metric_a": "steps",
                "metric_b": "mood",
                "start_date": iso(START),
                "end_date": iso(START + timedelta(days=39)),
            },
        )
    ).structured_content
    assert set(sc["claim"]) == {"evidence_a", "evidence_b", "profile", "decision"}
    assert sc["claim"]["evidence_a"] == sc["evidence_a"]
    assert sc["claim"]["evidence_b"] == sc["evidence_b"]
    assert sc["claim"]["profile"] == sc["evidence_profile"]
    assert sc["claim"]["decision"] == sc["claim_decision"]


async def test_correlation_schema_declares_claim_and_deprecates_legacy_fields(client):
    schema = {t.name: t for t in await client.list_tools()}["find_metric_correlation"].output_schema
    assert "claim" in schema["properties"] and "claim" in schema["required"]
    for legacy in ("evidence_a", "evidence_b", "evidence_profile", "claim_decision"):
        assert schema["properties"][legacy].get("deprecated") is True
        assert legacy in schema["required"]  # deprecated, not optional, during the migration window
    assert "sample_confidence" not in schema["properties"]


async def test_correlation_missingness_breaks_down_by_metric(client):
    """evidence_a/evidence_b's per-metric coverage facts aren't discarded by the ClaimEvidence
    migration — they're folded into profile.missingness, which reports each metric's own gaps
    (windows.metric_a / windows.metric_b) and takes the worse of the two as the dimension status."""
    await seed(client, "steps", [5000 + 10 * i for i in range(40)])
    # mood present every other day: sparse enough on its own to be the worse series, even though
    # every value it does have lines up with a "steps" day (so the paired count stays high).
    await seed(client, "mood", [1 + (i % 5) if i % 2 == 0 else None for i in range(40)])
    sc = (
        await client.call_tool(
            "find_metric_correlation",
            {
                "metric_a": "steps",
                "metric_b": "mood",
                "start_date": iso(START),
                "end_date": iso(START + timedelta(days=39)),
            },
        )
    ).structured_content
    windows = dims(sc)["missingness"]["details"]["windows"]
    assert set(windows) == {"metric_a", "metric_b"}
    assert windows["metric_a"]["status"] == "adequate"
    assert windows["metric_b"]["coverage"] == 0.5
    assert dims(sc)["missingness"]["status"] == windows["metric_b"]["status"]


# ---- anomaly detection ------------------------------------------------------------------------------------


async def test_anomaly_short_baseline_is_insufficient_but_same_spike_is_still_reported(client):
    spiky = [8000 + (i % 5) * 50 for i in range(60)]
    spiky[45] = 1000
    args = {"metric": "steps", "start_date": iso(START), "end_date": iso(START + timedelta(days=59))}
    await seed(client, "steps", spiky)
    full = (await client.call_tool("detect_metric_anomalies", args)).structured_content
    short = (
        await client.call_tool("detect_metric_anomalies", {**args, "start_date": iso(START + timedelta(days=40))})
    ).structured_content

    assert (
        [a["date"] for a in full["anomalies"]]
        == [a["date"] for a in short["anomalies"]]
        == [iso(START + timedelta(days=45))]
    )
    _, d_full = assert_pipeline_output(full, "anomaly")
    _, d_short = assert_pipeline_output(short, "anomaly")
    assert d_full["tier"] != "insufficient"
    assert d_short["tier"] == "insufficient" and "baseline_too_short" in d_short["must_state"]
    assert rank(short) < rank(full)


async def test_anomaly_recent_gap_limits_the_claim(client):
    await seed(client, "steps", gap_last([8000 + (i % 5) * 50 for i in range(60)], 8))
    sc = (
        await client.call_tool(
            "detect_metric_anomalies",
            {"metric": "steps", "start_date": iso(START), "end_date": iso(START + timedelta(days=59))},
        )
    ).structured_content
    _, decision = assert_pipeline_output(sc, "anomaly")
    assert "recent_window_gap" in decision["must_state"]


async def test_anomaly_claim_envelope_is_an_exact_mirror_of_the_legacy_fields(client):
    """Migration invariant: claim.{evidence,profile,decision} must equal the deprecated flat fields,
    not merely both be present. Prevents the canonical and legacy representations from diverging
    silently during the deprecation window."""
    await seed(client, "steps", [8000 + (i % 5) * 50 for i in range(60)])
    sc = (
        await client.call_tool(
            "detect_metric_anomalies",
            {"metric": "steps", "start_date": iso(START), "end_date": iso(START + timedelta(days=59))},
        )
    ).structured_content
    assert set(sc["claim"]) == {"evidence", "profile", "decision"}
    assert sc["claim"]["evidence"] == sc["evidence"]
    assert sc["claim"]["profile"] == sc["evidence_profile"]
    assert sc["claim"]["decision"] == sc["claim_decision"]


async def test_anomaly_schema_declares_claim_and_deprecates_legacy_fields(client):
    schema = {t.name: t for t in await client.list_tools()}["detect_metric_anomalies"].output_schema
    assert "claim" in schema["properties"] and "claim" in schema["required"]
    for legacy in ("evidence", "evidence_profile", "claim_decision"):
        assert schema["properties"][legacy].get("deprecated") is True
        assert legacy in schema["required"]  # deprecated, not optional, during the migration window


# ---- recent changes / explanatory analysis ----------------------------------------------------------------


async def test_recent_changes_every_note_carries_its_own_claim(client):
    today = date.today()
    recent_start = today - timedelta(days=7)
    await seed(client, "steps", [8000] * 28, recent_start - timedelta(days=28))
    await seed(client, "steps", [2000 + 400 * i for i in range(8)], recent_start)  # a shift and a clear trend
    sc = (await client.call_tool("get_recent_changes", {"days": 7})).structured_content
    by_kind = {n["kind"]: n for n in sc["changes"] if n["metric"] == "steps"}
    assert {"shift", "trend"} <= set(by_kind)
    for note in sc["changes"]:
        assert note["evidence_profile"] is not None and note["claim_decision"] is not None
    assert_pipeline_output(by_kind["shift"], "window_comparison")
    assert_pipeline_output(by_kind["trend"], "trend")
    # a 7-day trend cannot meet the registry's minimum n / span: the note is reported, but as insufficient
    assert by_kind["trend"]["claim_decision"]["tier"] == "insufficient"
    assert by_kind["shift"]["claim_decision"]["tier"] != "insufficient"


async def test_explain_metric_change_carries_a_claim_per_claim(client):
    n = 100
    steps = [8000 + (i % 5) * 400 for i in range(n)]
    mood = [3 + (i % 5) for i in range(n)]
    steps[-1], mood[-1] = 1000, 1  # the day being explained
    await seed(client, "steps", steps)
    await seed(client, "mood", mood)
    target = iso(START + timedelta(days=n - 1))
    sc = (await client.call_tool("explain_metric_change", {"metric": "steps", "date": target})).structured_content

    assert sc["is_anomaly"] is True
    assert_pipeline_output(sc, "anomaly")  # headline claim: this day vs its 90-day baseline
    assert_pipeline_output(sc, "trend", key_prefix="trend_")  # the 30-day trend is its own claim
    assert sc["correlated_metrics"], "fixture should produce at least one correlated metric"
    for c in sc["correlated_metrics"]:
        assert_pipeline_output(c, "correlation")

    # overall_decision is weakest-of-N over the headline claim, the trend claim, and every
    # surfaced correlation's own claim — never stronger than the weakest of the three groups.
    component_ranks = [
        TIER_RANK[ClaimTier(sc["claim_decision"]["tier"])],
        TIER_RANK[ClaimTier(sc["trend_claim_decision"]["tier"])],
        *(TIER_RANK[ClaimTier(c["claim_decision"]["tier"])] for c in sc["correlated_metrics"]),
    ]
    assert TIER_RANK[ClaimTier(sc["overall_decision"]["tier"])] == min(component_ranks)
    # every component's must_state that isn't adequate is folded into the composite's must_state
    all_factors = {
        f
        for f in sc["claim_decision"]["must_state"] + sc["trend_claim_decision"]["must_state"]
        for f in [f]
    } | {f for c in sc["correlated_metrics"] for f in c["claim_decision"]["must_state"]}
    assert all_factors <= set(sc["overall_decision"]["must_state"])


async def test_explain_metric_change_thin_history_is_insufficient(client):
    await seed(client, "steps", [8000 + (i % 5) * 400 for i in range(20)])
    sc = (
        await client.call_tool("explain_metric_change", {"metric": "steps", "date": iso(START + timedelta(days=19))})
    ).structured_content
    _, decision = assert_pipeline_output(sc, "anomaly")
    assert decision["tier"] == "insufficient" and "baseline_too_short" in decision["must_state"]
    # the headline claim alone is insufficient, so the composite can never be stronger than that
    assert sc["overall_decision"]["tier"] == "insufficient"


async def test_explain_metric_change_headline_and_trend_claims_are_exact_mirrors_of_the_legacy_fields(client):
    """P0.1 migration invariant: headline_claim.{profile,decision} must equal the deprecated flat
    evidence_profile/claim_decision, and trend_claim.{profile,decision} must equal the deprecated flat
    trend_evidence_profile/trend_claim_decision — not merely both be present. Prevents the canonical
    headline_claim/trend_claim envelopes and the legacy flat mirrors from diverging silently during the
    deprecation window (same invariant already enforced for trend/anomaly/compare_periods/correlation)."""
    n = 100
    steps = [8000 + (i % 5) * 400 for i in range(n)]
    mood = [3 + (i % 5) for i in range(n)]
    steps[-1], mood[-1] = 1000, 1
    await seed(client, "steps", steps)
    await seed(client, "mood", mood)
    target = iso(START + timedelta(days=n - 1))
    sc = (await client.call_tool("explain_metric_change", {"metric": "steps", "date": target})).structured_content

    assert set(sc["headline_claim"]) == {"evidence", "profile", "decision"}
    assert sc["headline_claim"]["profile"] == sc["evidence_profile"]
    assert sc["headline_claim"]["decision"] == sc["claim_decision"]

    assert set(sc["trend_claim"]) == {"evidence", "profile", "decision"}
    assert sc["trend_claim"]["profile"] == sc["trend_evidence_profile"]
    assert sc["trend_claim"]["decision"] == sc["trend_claim_decision"]


async def test_explain_metric_change_overall_decision_is_the_exact_weakest_of_n_of_its_components(client):
    """P0.1 acceptance condition: overall_decision must equal combine_decisions() applied to the
    headline claim, the trend claim, and every surfaced correlation's own decision — exact tier and
    exact must_state (sorted union of every component's must_state), not merely a superset/rank check."""
    n = 100
    steps = [8000 + (i % 5) * 400 for i in range(n)]
    mood = [3 + (i % 5) for i in range(n)]
    steps[-1], mood[-1] = 1000, 1
    await seed(client, "steps", steps)
    await seed(client, "mood", mood)
    target = iso(START + timedelta(days=n - 1))
    sc = (await client.call_tool("explain_metric_change", {"metric": "steps", "date": target})).structured_content

    component_decisions = [
        sc["claim_decision"],
        sc["trend_claim_decision"],
        *(c["claim_decision"] for c in sc["correlated_metrics"]),
    ]
    expected_tier = min(component_decisions, key=lambda d: TIER_RANK[ClaimTier(d["tier"])])["tier"]
    expected_must_state = sorted({f for d in component_decisions for f in d["must_state"]})

    assert sc["overall_decision"]["tier"] == expected_tier
    assert sc["overall_decision"]["must_state"] == expected_must_state


async def test_explain_metric_change_schema_declares_claim_envelopes_and_deprecates_legacy_fields(client):
    schema = {t.name: t for t in await client.list_tools()}["explain_metric_change"].output_schema
    for envelope in ("headline_claim", "trend_claim", "overall_decision"):
        assert envelope in schema["properties"] and envelope in schema["required"]
    for legacy in ("evidence_profile", "claim_decision", "trend_evidence_profile", "trend_claim_decision"):
        assert schema["properties"][legacy].get("deprecated") is True
        assert legacy in schema["required"]  # deprecated, not optional, during the migration window


# ---- baseline ---------------------------------------------------------------------------------------------


async def test_baseline_carries_a_claim_envelope_and_short_windows_are_insufficient(client):
    series = [8000 + (i % 5) * 50 for i in range(60)]
    args = {"metric": "steps", "start_date": iso(START), "end_date": iso(START + timedelta(days=59))}
    await seed(client, "steps", series)
    full = (await client.call_tool("get_baseline", args)).structured_content
    short = (
        await client.call_tool("get_baseline", {**args, "start_date": iso(START + timedelta(days=45))})
    ).structured_content

    for sc in (full, short):
        claim = sc["claim"]
        assert set(claim) == {"evidence", "profile", "decision"}
        assert claim["profile"]["analysis"] == "baseline"
        assert {d["dimension"] for d in claim["profile"]["dimensions"]} == {d.value for d in REG["baseline"].dimensions}
        assert claim["decision"]["tier"] in {t.value for t in ClaimTier} and claim["decision"]["template"]
        assert claim["evidence"] == sc["evidence"]  # legacy field stays identical during the deprecation release
    assert full["claim"]["decision"]["tier"] == "supported" and full["claim"]["decision"]["must_state"] == []
    assert short["claim"]["decision"]["tier"] == "insufficient"
    assert "baseline_too_short" in short["claim"]["decision"]["must_state"]
    assert full["baseline"]["n"] == 60 and short["baseline"]["n"] == 15  # statistics themselves are unaffected


async def test_baseline_with_no_data_is_insufficient_not_an_error(client):
    args = {"metric": "steps", "start_date": iso(START), "end_date": iso(START + timedelta(days=29))}
    sc = (await client.call_tool("get_baseline", args)).structured_content
    assert sc["baseline"]["n"] == 0
    assert sc["claim"]["decision"]["tier"] == "insufficient"


async def test_baseline_schema_declares_claim_and_deprecates_legacy_evidence(client):
    schema = {t.name: t for t in await client.list_tools()}["get_baseline"].output_schema
    assert "claim" in schema["properties"] and "claim" in schema["required"]
    assert schema["properties"]["evidence"].get("deprecated") is True


# ---- contract: nothing analytical bypasses the pipeline ---------------------------------------------------


async def test_every_analytical_result_schema_declares_both_fields(client):
    tools = {t.name: t for t in await client.list_tools()}

    def props(node):
        """Every property name anywhere in the schema (FastMCP inlines nested models)."""
        found = set()
        if isinstance(node, dict):
            found |= set(node.get("properties", {}))
            for v in node.values():
                found |= props(v)
        elif isinstance(node, list):
            for v in node:
                found |= props(v)
        return found

    for name in (
        "detect_metric_anomalies",
        "calculate_metric_trend",
        "compare_metric_periods",
        "find_metric_correlation",
        "get_recent_changes",
        "explain_metric_change",
    ):
        p = props(tools[name].output_schema)
        assert {"evidence_profile", "claim_decision"} <= p, name
