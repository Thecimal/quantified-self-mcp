"""ClaimEvidence contract, adversarially, at the MCP boundary.

Every public analytics tool (get_baseline, calculate_metric_trend, detect_metric_anomalies,
compare_metric_periods, find_metric_correlation, get_recent_changes, explain_metric_change) is called through a
real MCP client over degraded data. For every claim in every response:

  * the profile lists every dimension the registry requires for that analysis (nothing silently absent);
  * the decision is exactly what the canonical resolver derives from that profile;
  * a profile with any weak/concern/blocking/not-assessed dimension is never "supported", and a blocking one is
    always "insufficient";
  * the claim is never stronger than the scenario justifies (explicit per-scenario ceilings below).

Statistical outputs are asserted unchanged by degraded provenance: only the claim strength moves.
"""

from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

import pytest
from fastmcp import Client
from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from qs_evidence import (  # noqa: E402
    DimensionResult,
    EvidenceProfile,
    Status,
    load_registry,
    resolve,
    validate_claim_decision,
)
from qs_evidence.models import TIER_RANK, ClaimTier, Dimension  # noqa: E402

REG = load_registry()
TODAY = date.today()
INSUFFICIENT, SUGGESTIVE, SUPPORTED = (ClaimTier.INSUFFICIENT, ClaimTier.SUGGESTIVE, ClaimTier.SUPPORTED)


def d(offset: int) -> date:
    """The day `offset` days before today."""
    return TODAY - timedelta(days=offset)


def noisy(i: int, base: int = 5000) -> int:
    return base + ((i * 7) % 11) * 100


# ---- harness -------------------------------------------------------------------------------------------


@pytest.fixture
def server_mod(tmp_path, monkeypatch):
    monkeypatch.setenv("HEALTH_DB_PATH", str(tmp_path / "health.db"))
    for mod in ("server", "privacy", "tools.health", "tools.measurements"):
        sys.modules.pop(mod, None)
    import server

    return server


@pytest.fixture
async def client(server_mod):
    async with Client(server_mod.mcp) as c:
        yield c


def seed(server, metric: str, offsets, value=noisy) -> None:
    for i in offsets:
        server.log_daily_metric(date=d(i).isoformat(), **{metric: value(i)})


def seed_sources(server, metric: str, offsets, source_for) -> None:
    """Raw measurements, so several sources can report the same day (see source_for(i) -> [(source, value)])."""
    for i in offsets:
        for source, value in source_for(i):
            server.log_measurement(timestamp=f"{d(i).isoformat()}T08:00:00", metric=metric, value=value, source=source)


def window(n: int = 60) -> dict:
    return {"start_date": d(n - 1).isoformat(), "end_date": d(0).isoformat()}


def claims_in(obj) -> list[dict]:
    """Every claim envelope (a dict with profile + decision) anywhere in a tool response."""
    found: list[dict] = []
    if isinstance(obj, dict):
        if "profile" in obj and "decision" in obj:
            found.append(obj)
        for v in obj.values():
            found += claims_in(v)
    elif isinstance(obj, list):
        for v in obj:
            found += claims_in(v)
    return found


def tier(decision: dict) -> ClaimTier:
    return ClaimTier(decision["tier"])


def assert_claim_is_sound(claim: dict) -> None:
    profile = EvidenceProfile.model_validate(claim["profile"])
    spec = REG[profile.analysis]
    # nothing the registry requires is silently absent
    assert {x.dimension for x in profile.dimensions} == set(spec.dimensions), profile.analysis
    # the decision is exactly the resolver's, so it cannot be stronger or stripped of caveats
    validate_claim_decision(profile, claim["decision"])
    statuses = {x.dimension: x for x in profile.dimensions}
    blocking = any(x.status == Status.BLOCKING and x.can_block for x in statuses.values())
    degraded = any(
        x.status in (Status.WEAK, Status.CONCERN, Status.BLOCKING, Status.NOT_ASSESSED) for x in statuses.values()
    )
    if degraded:
        assert tier(claim["decision"]) != SUPPORTED
    if blocking:
        assert tier(claim["decision"]) == INSUFFICIENT
    if spec.max_tier is not None:
        assert TIER_RANK[tier(claim["decision"])] <= TIER_RANK[spec.max_tier.tier]


def assert_all_sound(response: dict) -> list[dict]:
    claims = claims_in(response)
    for c in claims:
        assert_claim_is_sound(c)
    return claims


def assert_at_most(claim: dict, ceiling: ClaimTier) -> None:
    assert TIER_RANK[tier(claim["decision"])] <= TIER_RANK[ceiling], claim["decision"]


async def call(client, name: str, args: dict) -> dict:
    return (await client.call_tool(name, args)).structured_content


# ---- single-window tools over degraded datasets ----------------------------------------------------------

# (scenario, offsets that hold a value) -> ceiling per tool. Ceilings are what the data can justify, taken from
# the registry thresholds (min_n 14, min_span 21, min_baseline_days 28, ...); none was loosened for a test.
SCENARIOS = {
    "empty": ([], INSUFFICIENT),
    "one_observation": ([30], INSUFFICIENT),
    "sparse": ([5, 20, 33, 41, 52, 58], INSUFFICIENT),
    "large_gap_then_data": (list(range(59, 39, -1)) + list(range(14, -1, -1)), SUGGESTIVE),
    "stale_data_ended_long_ago": (list(range(59, 39, -1)), SUGGESTIVE),
}
SINGLE_WINDOW_TOOLS = ["get_baseline", "calculate_metric_trend", "detect_metric_anomalies"]


@pytest.mark.parametrize("tool", SINGLE_WINDOW_TOOLS)
@pytest.mark.parametrize("scenario", sorted(SCENARIOS))
async def test_degraded_datasets_never_yield_a_stronger_claim_than_the_data_justifies(
    client, server_mod, tool, scenario
):
    offsets, ceiling = SCENARIOS[scenario]
    seed(server_mod, "steps", offsets)
    out = await call(client, tool, {"metric": "steps", **window()})
    claims = assert_all_sound(out)
    assert len(claims) == 1, "every analytical result must carry exactly its canonical claim"
    assert_at_most(claims[0], ceiling)
    # the statistical output is still there, separate from the claim-strength decision
    assert ("baseline" in out) or ("trend" in out) or ("anomalies" in out)


@pytest.mark.parametrize("tool", SINGLE_WINDOW_TOOLS)
async def test_constant_series_is_capped_for_trend_and_blocked_for_anomalies(client, server_mod, tool):
    seed(server_mod, "steps", range(59, -1, -1), value=lambda i: 5000)
    out = await call(client, tool, {"metric": "steps", **window()})
    (claim,) = assert_all_sound(out)
    if tool == "detect_metric_anomalies":
        assert tier(claim["decision"]) == INSUFFICIENT
        assert "baseline_zero_variance" in claim["decision"]["must_state"]
    elif tool == "calculate_metric_trend":
        assert_at_most(claim, SUGGESTIVE)


# ---- conflicting sources: the decision, not only data_health, must carry them -----------------------------


def _conflicting(server, metric="mood", n=60):
    seed_sources(server, metric, range(n - 1, -1, -1), lambda i: [("appA", 5 + i % 3), ("appB", 8 - i % 3)])


def _source_change(server, metric="mood", n=60):
    seed_sources(
        server,
        metric,
        range(n - 1, -1, -1),
        lambda i: [("watch", 5 + i % 3)] if i >= n // 2 else [("phone", 5 + i % 3)],
    )


@pytest.mark.parametrize("tool", SINGLE_WINDOW_TOOLS)
@pytest.mark.parametrize(
    ("build", "code"),
    [(_conflicting, "unranked_source_conflict_in_window"), (_source_change, "source_changed_within_window")],
)
async def test_conflicting_or_changing_sources_downgrade_the_decision_itself(client, server_mod, tool, build, code):
    build(server_mod)
    out = await call(client, tool, {"metric": "mood", **window()})
    (claim,) = assert_all_sound(out)
    assert tier(claim["decision"]) != SUPPORTED
    assert code in claim["decision"]["must_state"]
    prov = next(x for x in claim["profile"]["dimensions"] if x["dimension"] == "provenance")
    assert prov["status"] == "weak"


async def test_the_reported_regression_baseline_over_an_all_conflict_window_is_no_longer_supported(client, server_mod):
    """get_baseline used to return tier=supported, must_state=[] here while data_health said MIXED_SOURCES."""
    _conflicting(server_mod)
    out = await call(client, "get_baseline", {"metric": "mood", **window()})
    assert out["data_health"]["status"] == "MIXED_SOURCES"
    assert out["claim"]["decision"]["tier"] == "suggestive"
    assert out["claim"]["decision"]["must_state"] == ["unranked_source_conflict_in_window"]


async def test_degraded_provenance_moves_only_the_claim_never_the_statistics(client, server_mod):
    _conflicting(server_mod)
    out = await call(client, "get_baseline", {"metric": "mood", **window()})
    assert out["claim"]["decision"]["tier"] == "suggestive"
    # the numbers are exactly what the analytics layer computes from the stored series, claim or no claim
    start, end = d(59), d(0)
    expected = server_mod._baseline_stats(server_mod._fetch_metric_series("mood", start, end)).model_dump()
    assert out["baseline"] == expected
    assert out["baseline"]["n"] == 60
    assert out["claim"]["profile"]["effect"] == expected


async def test_an_unreadable_provenance_read_caps_the_claim_instead_of_passing_it(client, server_mod, monkeypatch):
    seed(server_mod, "steps", range(59, -1, -1))
    monkeypatch.setattr(server_mod, "_provenance_for", lambda requests: [None] * len(requests))
    out = await call(client, "get_baseline", {"metric": "steps", **window()})
    (claim,) = assert_all_sound(out)
    assert tier(claim["decision"]) == SUGGESTIVE
    assert "provenance_not_assessed" in claim["decision"]["must_state"]


# ---- period comparison -------------------------------------------------------------------------------------


def _periods(a_days: int, b_days: int) -> dict:
    return {
        "period_a_start": d(a_days - 1).isoformat(),
        "period_a_end": d(0).isoformat(),
        "period_b_start": d(a_days + b_days).isoformat(),
        "period_b_end": d(a_days + 1).isoformat(),
    }


@pytest.mark.parametrize(
    ("a_days", "b_days", "a_obs", "b_obs", "ceiling"),
    [
        (14, 14, 0, 0, INSUFFICIENT),  # both empty
        (14, 14, 1, 14, INSUFFICIENT),  # one observation in A
        (3, 60, 3, 60, INSUFFICIENT),  # unequal windows: A too short for min_n_per_window
        (14, 60, 14, 60, SUGGESTIVE),  # unequal but each adequate: the ceiling still applies
        (14, 14, 14, 3, INSUFFICIENT),  # baseline period nearly empty
    ],
)
async def test_period_comparison_is_never_stronger_than_its_weakest_window(
    client, server_mod, a_days, b_days, a_obs, b_obs, ceiling
):
    seed(server_mod, "steps", range(a_obs))
    seed(server_mod, "steps", range(a_days + 1, a_days + 1 + b_obs), value=lambda i: noisy(i, 4000))
    out = await call(client, "compare_metric_periods", {"metric": "steps", **_periods(a_days, b_days)})
    (claim,) = assert_all_sound(out)
    assert_at_most(claim, ceiling)
    assert claim["profile"]["analysis"] == "window_comparison"
    assert "delta" in out and "period_a_stats" in out  # statistics preserved beside the decision


async def test_period_comparison_across_a_source_switch_is_flagged(client, server_mod):
    seed_sources(server_mod, "steps", range(13, -1, -1), lambda i: [("watch", noisy(i))])
    seed_sources(server_mod, "steps", range(28, 14, -1), lambda i: [("phone", noisy(i, 4000))])
    out = await call(client, "compare_metric_periods", {"metric": "steps", **_periods(14, 14)})
    (claim,) = assert_all_sound(out)
    assert "source_changed_between_windows" in claim["decision"]["must_state"]
    assert tier(claim["decision"]) != SUPPORTED


# ---- correlation ---------------------------------------------------------------------------------------------


async def test_correlation_with_no_paired_days_is_insufficient(client, server_mod):
    seed(server_mod, "steps", range(59, -1, -2))
    seed(server_mod, "sleep_hours", range(58, -1, -2), value=lambda i: 6 + (i % 4) * 0.2)
    out = await call(client, "find_metric_correlation", {"metric_a": "steps", "metric_b": "sleep_hours", **window()})
    (claim,) = assert_all_sound(out)
    assert tier(claim["decision"]) == INSUFFICIENT


@pytest.mark.parametrize("pairs", [5, 12, 19])
async def test_correlation_below_the_paired_minimum_is_insufficient_whatever_r_says(client, server_mod, pairs):
    seed(server_mod, "steps", range(59, -1, -1))
    seed(server_mod, "sleep_hours", range(pairs), value=lambda i: 5 + noisy(i) / 1000)  # perfectly tracks steps
    out = await call(client, "find_metric_correlation", {"metric_a": "steps", "metric_b": "sleep_hours", **window()})
    (claim,) = assert_all_sound(out)
    assert tier(claim["decision"]) == INSUFFICIENT
    assert out["r"] is None or abs(out["r"]) > 0.9  # the strong r is reported, the claim is still capped


async def test_correlation_with_a_constant_series_is_never_supported(client, server_mod):
    seed(server_mod, "steps", range(59, -1, -1))
    seed(server_mod, "sleep_hours", range(59, -1, -1), value=lambda i: 7.0)
    out = await call(client, "find_metric_correlation", {"metric_a": "steps", "metric_b": "sleep_hours", **window()})
    (claim,) = assert_all_sound(out)
    assert_at_most(claim, SUGGESTIVE)
    assert out["r"] is None


async def test_correlation_with_one_conflicted_series_names_the_conflict(client, server_mod):
    seed(server_mod, "steps", range(59, -1, -1))
    _conflicting(server_mod, metric="mood")
    out = await call(client, "find_metric_correlation", {"metric_a": "steps", "metric_b": "mood", **window()})
    (claim,) = assert_all_sound(out)
    assert "unranked_source_conflict_in_window" in claim["decision"]["must_state"]


# ---- composites -----------------------------------------------------------------------------------------------


async def test_explain_overall_is_the_weakest_component_when_one_correlation_is_insufficient(client, server_mod):
    seed(server_mod, "steps", range(95, -1, -1))
    # a sparse metric that tracks steps exactly: surfaces as a correlated metric, with too few pairs to back it
    seed(server_mod, "sleep_hours", range(1, 7), value=lambda i: 5 + noisy(i) / 1000)
    out = await call(client, "explain_metric_change", {"metric": "steps", "date": d(0).isoformat()})
    claims = assert_all_sound(out)
    assert out["correlated_metrics"], "the scenario must surface a correlation, or it proves nothing"
    components = [tier(c["decision"]) for c in claims]
    assert INSUFFICIENT in components
    assert tier(out["overall_decision"]) == INSUFFICIENT
    assert min(TIER_RANK[t] for t in components) == TIER_RANK[tier(out["overall_decision"])]


async def test_explain_overall_inherits_a_source_conflict_in_the_metric(client, server_mod):
    _conflicting(server_mod, metric="mood", n=96)
    out = await call(client, "explain_metric_change", {"metric": "mood", "date": d(0).isoformat()})
    assert_all_sound(out)
    assert "unranked_source_conflict_in_window" in out["baseline_claim"]["decision"]["must_state"]
    assert "unranked_source_conflict_in_window" in out["overall_decision"]["must_state"]
    assert tier(out["overall_decision"]) != SUPPORTED


async def test_explain_on_a_day_with_no_value_and_no_history_is_insufficient_throughout(client, server_mod):
    out = await call(client, "explain_metric_change", {"metric": "steps", "date": d(0).isoformat()})
    assert_all_sound(out)
    assert out["value"] is None
    assert tier(out["overall_decision"]) == INSUFFICIENT


async def test_recent_changes_notes_each_carry_a_sound_claim_and_inherit_source_conflicts(client, server_mod):
    # baseline period [d(35), d(8)] steady; recent period [d(7), d(0)] up sharply, every day with two unranked sources
    seed(server_mod, "steps", range(35, 7, -1))
    seed_sources(server_mod, "steps", range(7, -1, -1), lambda i: [("appA", 9000 + i * 50), ("appB", 9400 + i * 50)])
    seed(server_mod, "hrv_ms", range(35, -1, -1), value=lambda i: 50 + noisy(i) / 1000)
    out = await call(client, "get_recent_changes", {"days": 7})
    assert out["changes"], "the scenario must produce notes, or it proves nothing"
    assert_all_sound(out)
    steps_notes = [c for c in out["changes"] if c["metric"] == "steps"]
    assert steps_notes
    for note in steps_notes:
        assert "claim" in note and note["claim"]["decision"]["tier"] != "supported"
        assert "unranked_source_conflict_in_window" in note["claim"]["decision"]["must_state"]
    for note in out["changes"]:
        assert note["claim"]["decision"]["tier"] in {t.value for t in ClaimTier}  # present on every note
        assert_at_most(note["claim"], SUGGESTIVE)


async def test_recent_changes_on_an_empty_database_reports_no_unjustified_claims(client, server_mod):
    out = await call(client, "get_recent_changes", {"days": 7})
    assert out["changes"] == []
    assert assert_all_sound(out) == []


# ---- malformed or missing evidence profiles, at the schema boundary ----------------------------------------


def _dims(**statuses: Status) -> list[DimensionResult]:
    return [
        DimensionResult(dimension=Dimension(n), status=s, reason_codes=[] if s == Status.ADEQUATE else [f"{n}_reason"])
        for n, s in statuses.items()
    ]


def test_a_profile_missing_the_provenance_dimension_cannot_carry_a_supported_decision(server_mod):
    import schemas

    profile = EvidenceProfile(
        metric="m",
        analysis="baseline",
        dimensions=_dims(sample=Status.ADEQUATE, temporal=Status.ADEQUATE, missingness=Status.ADEQUATE),
    )
    honest = resolve(profile, REG["baseline"]).to_mcp()
    assert honest["tier"] == "suggestive" and "provenance_not_assessed" in honest["must_state"]
    claimed = {**honest, "tier": "supported", "permitted_phrasing_class": "supported", "must_state": []}
    with pytest.raises(ValidationError, match="does not match"):
        schemas.ClaimEvidence(
            evidence=schemas.Evidence.model_construct(),
            profile=profile,
            decision=schemas.ClaimDecisionOut(**claimed),
        )


def test_a_decision_with_a_dropped_provenance_caveat_is_rejected(server_mod):
    import schemas

    profile = EvidenceProfile(
        metric="m",
        analysis="baseline",
        dimensions=_dims(
            sample=Status.ADEQUATE, temporal=Status.ADEQUATE, missingness=Status.ADEQUATE, provenance=Status.WEAK
        ),
    )
    honest = resolve(profile, REG["baseline"]).to_mcp()
    assert honest["must_state"] == ["provenance_reason"]
    with pytest.raises(ValidationError, match="does not match"):
        schemas.ClaimEvidence(
            evidence=schemas.Evidence.model_construct(),
            profile=profile,
            decision=schemas.ClaimDecisionOut(**{**honest, "must_state": []}),
        )


def test_an_unknown_analysis_is_an_error_never_a_pass():
    profile = EvidenceProfile(metric="m", analysis="not_registered", dimensions=_dims(sample=Status.ADEQUATE))
    with pytest.raises(ValueError, match="unknown analysis"):
        validate_claim_decision(
            profile,
            {"tier": "supported", "permitted_phrasing_class": "supported", "must_state": [], "template": ""},
        )
