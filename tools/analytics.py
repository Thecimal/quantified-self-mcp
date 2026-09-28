"""
Analytics tools
===============

get_metric_history, get_baseline, detect_metric_anomalies,
calculate_metric_trend, compare_metric_periods, and find_metric_correlation,
moved out of server.py verbatim. Each tool body is unchanged; the one
server.py global/helper that cannot be imported without a circular dependency
(_fetch_metric_series, which needs _readonly_connection and HEALTH_DB_PATH)
is closure-bound as fetch_metric_series, matching the pattern used by
tools/health.py and tools/measurements.py. Pure helper functions
(_baseline_stats, _trend_stats, _claim_evidence, etc.) are defined here
directly — they depend only on schemas and qs_evidence, not on server.py.
Server.py keeps its own copies of the shared helpers so Layer-3 tools can
call them without a circular import. See server.py for the call that wires
these back in.
"""

from collections.abc import Callable
from datetime import date as date_type

from mcp.types import ToolAnnotations

from analytics import (
    Point,
    calculate_trend,
    compare_periods,
    detect_anomalies,
    find_correlations,
)
from analytics import baseline as compute_baseline
from errors import (
    ERR_INVALID_RANGE,
    _tool_error,
)
from evidence import build_evidence
from logic import (
    resolve_range,
)
from qs_evidence import (
    Assessment,
    assess_anomaly,
    assess_baseline,
    assess_correlation,
    assess_trend,
    assess_window_comparison,
)
from schemas import (
    AnomalyPoint,
    BaselineStats,
    CalculateTrendResult,
    ClaimDecisionOut,
    ClaimEvidence,
    ClaimEvidenceComparative,
    ComparePeriodsResult,
    CorrelationResult,
    DateRange,
    DetectAnomaliesResult,
    Evidence,
    GetBaselineResult,
    GetMetricHistoryResult,
    MetricSeriesPoint,
    TrendStats,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
# These pure functions depend only on schemas/qs_evidence. Server.py keeps
# its own copies so Layer-3 tools can call them without importing from here
# (which would create a circular import: server.py -> tools/analytics.py ->
# server.py).


def _baseline_stats(series: list[Point]) -> BaselineStats:
    return BaselineStats(**compute_baseline(series))


def _trend_stats(series: list[Point]) -> TrendStats:
    return TrendStats(**calculate_trend(series))


def _claim_evidence(assessment: Assessment, evidence: Evidence) -> ClaimEvidence:
    """Canonical ClaimEvidence envelope for one assessed claim."""
    return ClaimEvidence(
        evidence=evidence,
        profile=assessment.profile,
        decision=ClaimDecisionOut(**assessment.decision.to_mcp()),
    )


def _migrated_claim_fields(assessment: Assessment, evidence: Evidence) -> dict:
    """Result-model kwargs for a tool that has adopted ClaimEvidence: the canonical `claim` envelope
    plus the deprecated flat fields, built as exact mirrors of `claim` (never independently derived)
    so the two representations cannot silently diverge during the deprecation window. Used by
    detect_metric_anomalies, calculate_metric_trend, and each ChangeNote built in get_recent_changes
    (get_baseline builds `claim` directly instead). `_claim_fields` is now unused dead code, kept only
    until ClaimFields itself is removed in the post-deprecation cleanup pass."""
    claim = _claim_evidence(assessment, evidence)
    return {
        "claim": claim,
        "evidence_profile": claim.profile,
        "claim_decision": claim.decision,
    }


def _claim_evidence_comparative(
    assessment: Assessment, evidence_a: Evidence, evidence_b: Evidence
) -> ClaimEvidenceComparative:
    """Canonical ClaimEvidenceComparative envelope for one assessed claim resting on two source coverages."""
    return ClaimEvidenceComparative(
        evidence_a=evidence_a,
        evidence_b=evidence_b,
        profile=assessment.profile,
        decision=ClaimDecisionOut(**assessment.decision.to_mcp()),
    )


def _migrated_claim_fields_comparative(assessment: Assessment, evidence_a: Evidence, evidence_b: Evidence) -> dict:
    """Result-model kwargs for a two-source tool that has adopted ClaimEvidenceComparative: the canonical
    `claim` envelope plus the deprecated flat fields, built as exact mirrors of `claim` (never independently
    derived) so the two representations cannot silently diverge during the deprecation window. Used by
    find_metric_correlation (ComparePeriodsResult builds `claim` via `_claim_evidence_comparative` directly,
    and derives its own differently-named deprecated evidence_a/evidence_b-equivalent fields instead)."""
    claim = _claim_evidence_comparative(assessment, evidence_a, evidence_b)
    return {
        "claim": claim,
        "evidence_a": claim.evidence_a,
        "evidence_b": claim.evidence_b,
        "evidence_profile": claim.profile,
        "claim_decision": claim.decision,
    }


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def register_analytics_tools(
    mcp,
    *,
    fetch_metric_series: Callable[[str, date_type, date_type], list[Point]],
) -> None:
    """Register all Layer-2 analytics tools on *mcp*.

    fetch_metric_series must be server.py's _fetch_metric_series — injected
    here to avoid a circular import (tools/analytics.py cannot import from
    server.py while server.py imports from tools/analytics.py).
    """

    @mcp.tool(
        annotations=ToolAnnotations(
            title="Get metric history",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        )
    )
    def get_metric_history(
        metric: str, start_date: str | None = None, end_date: str | None = None
    ) -> GetMetricHistoryResult:
        """
        Read one metric's day-by-day values, without the other six metrics
        read_health_data always includes. Use this when you only care about a
        single metric (e.g. before calling get_baseline or calculate_metric_trend
        yourself) and don't need the full multi-metric payload.

        Use this tool when:
        - the user wants one specific metric's history/trend over time (e.g.
          "show my weight over the last 30 days", "how has resting heart rate
          changed this month").

        Do not use this tool when:
        - the request is broad/general, across multiple metrics at once -> use
          `read_health_data` instead.

        Privacy note: this server and its SQLite file are entirely local, but
        the data returned by this tool becomes part of the conversation sent
        to whatever model the calling client is configured with. If that
        model runs in the cloud rather than on your machine, treat this the
        same as pasting the data into a chat with that provider.

        Args:
            metric: One of steps, sleep_hours, resting_heart_rate, weight_kg,
                workout_minutes, mood, water_ml, heart_rate, hrv_ms. Rejected if configured as
                private via HEALTH_PRIVATE_FIELDS.
            start_date: First day to include, formatted YYYY-MM-DD.
                Defaults to 30 days before end_date.
            end_date: Last day to include, formatted YYYY-MM-DD. Defaults to today.

        Returns:
            A GetMetricHistoryResult with "points" (date/value pairs; days with
            no recorded value for this metric are simply absent).
        """
        try:
            start, end = resolve_range(start_date, end_date, default_days=30)
        except ValueError as exc:
            raise _tool_error(ERR_INVALID_RANGE, str(exc)) from exc
        series = fetch_metric_series(metric, start, end)
        return GetMetricHistoryResult(
            metric=metric,
            range=DateRange(start_date=start.isoformat(), end_date=end.isoformat()),
            points=[MetricSeriesPoint(date=p.day.isoformat(), value=p.value) for p in series],
            evidence=Evidence(**build_evidence(series, start, end)),
        )

    @mcp.tool(
        annotations=ToolAnnotations(
            title="Get metric baseline",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        )
    )
    def get_baseline(metric: str, start_date: str | None = None, end_date: str | None = None) -> GetBaselineResult:
        """
        Compute "what's normal" for one metric over a window: mean, median,
        and standard deviation. This is the number every other analytics tool
        below measures against, so a wider window (60-90+ days) gives a more
        stable baseline than the 30-day default read_health_data uses.

        Use this tool when:
        - the user asks what's normal/typical/usual for one metric (e.g.
          "what's normal for my resting heart rate?"), with no particular
          day or direction of change in mind.

        Do not use this tool when:
        - the user asks whether a metric is going up/down over time -> use
          `calculate_metric_trend` instead.
        - the user asks whether specific days looked unusual -> use
          `detect_metric_anomalies` instead.
        - the user wants two ranges compared against each other -> use
          `compare_metric_periods` instead.

        Privacy note: this server and its SQLite file are entirely local, but
        the data returned by this tool becomes part of the conversation sent
        to whatever model the calling client is configured with. If that
        model runs in the cloud rather than on your machine, treat this the
        same as pasting the data into a chat with that provider.

        Args:
            metric: One of steps, sleep_hours, resting_heart_rate, weight_kg,
                workout_minutes, mood, water_ml, heart_rate, hrv_ms.
            start_date: First day to include, formatted YYYY-MM-DD.
                Defaults to 90 days before end_date.
            end_date: Last day to include, formatted YYYY-MM-DD. Defaults to today.

        Returns:
            A GetBaselineResult with "baseline" (mean/median/stdev/n) plus
            "claim" — the evidence behind it (claim.evidence: how much of the
            window it is based on; claim.profile: per-dimension quality) and
            claim.decision, which says how strongly the baseline may be stated.
            Report every entry in claim.decision.must_state (e.g. "based on only
            60% of days") rather than stating mean/median as if they were
            computed from a complete series. All baseline fields are null and n
            is 0 if the metric has no data in range — not an error, since
            "nothing logged yet" is an expected state.
        """
        try:
            start, end = resolve_range(start_date, end_date, default_days=90)
        except ValueError as exc:
            raise _tool_error(ERR_INVALID_RANGE, str(exc)) from exc
        series = fetch_metric_series(metric, start, end)
        stats = _baseline_stats(series)
        evidence = Evidence(**build_evidence(series, start, end))
        assessment = assess_baseline(metric, series, start, end, effect=stats.model_dump())
        return GetBaselineResult(
            metric=metric,
            range=DateRange(start_date=start.isoformat(), end_date=end.isoformat()),
            baseline=stats,
            claim=_claim_evidence(assessment, evidence),
            evidence=evidence,
        )

    @mcp.tool(
        annotations=ToolAnnotations(
            title="Detect metric anomalies",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        )
    )
    def detect_metric_anomalies(
        metric: str, start_date: str | None = None, end_date: str | None = None, threshold: float = 3.5
    ) -> DetectAnomaliesResult:
        """
        Flag days where one metric deviated sharply from its own baseline over
        the window, using a median/MAD-based modified z-score rather than a
        mean/stdev z-score — more robust for short, noisy personal-health
        series, where the mean/stdev version is easily dragged around by the
        very outliers it's supposed to catch.

        Use this tool when:
        - the user asks whether anything looked unusual/off/weird on
          particular days for one metric (e.g. "was there anything unusual
          about my sleep last month?").

        Do not use this tool when:
        - the user wants a general sense of what's typical, with no interest
          in flagging specific days -> use `get_baseline` instead.
        - the user asks about a steady increase/decrease over time rather
          than isolated spikes/dips -> use `calculate_metric_trend` instead.
        - the user already has one specific date in mind and wants the full
          "why" behind it (baseline, anomaly, trend, and correlations
          together) -> use `explain_metric_change` instead.

        Privacy note: this server and its SQLite file are entirely local, but
        the data returned by this tool becomes part of the conversation sent
        to whatever model the calling client is configured with. If that
        model runs in the cloud rather than on your machine, treat this the
        same as pasting the data into a chat with that provider.

        Args:
            metric: One of steps, sleep_hours, resting_heart_rate, weight_kg,
                workout_minutes, mood, water_ml, heart_rate, hrv_ms.
            start_date: First day to include, formatted YYYY-MM-DD.
                Defaults to 90 days before end_date.
            end_date: Last day to include, formatted YYYY-MM-DD. Defaults to today.
            threshold: Modified z-score cutoff. 3.5 (the default, Iglewicz &
                Hoaglin's standard value) flags only clear outliers; lower it
                (e.g. 2.5) to see more borderline days.

        Returns:
            A DetectAnomaliesResult with "anomalies" (empty if fewer than 5
            days have data, or if the metric has no meaningful spread) plus
            "evidence" for the window they were computed over. An empty
            "anomalies" list with evidence.confidence "moderate" or "low"
            means "not enough data to tell," not "nothing unusual happened" —
            say that explicitly rather than reporting a clean bill of health.
        """
        try:
            start, end = resolve_range(start_date, end_date, default_days=90)
        except ValueError as exc:
            raise _tool_error(ERR_INVALID_RANGE, str(exc)) from exc
        series = fetch_metric_series(metric, start, end)
        anomalies = detect_anomalies(series, threshold=threshold)
        assessment = assess_anomaly(
            metric, series, start, end, effect={"n_anomalies": len(anomalies), "threshold": threshold}
        )
        evidence = Evidence(**build_evidence(series, start, end))
        return DetectAnomaliesResult(
            metric=metric,
            range=DateRange(start_date=start.isoformat(), end_date=end.isoformat()),
            threshold=threshold,
            anomalies=[AnomalyPoint(**a) for a in anomalies],
            evidence=evidence,
            **_migrated_claim_fields(assessment, evidence),
        )

    @mcp.tool(
        annotations=ToolAnnotations(
            title="Calculate metric trend",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        )
    )
    def calculate_metric_trend(
        metric: str, start_date: str | None = None, end_date: str | None = None
    ) -> CalculateTrendResult:
        """
        Fit a simple straight-line trend to one metric over a window and
        report its direction, slope (change per day), and r_squared (how well
        a straight line actually fits — low r_squared means "noisy," not
        "flat").

        Use this tool when:
        - the user asks whether one metric is trending up/down/flat over a
          continuous window (e.g. "is my weight trending down?", "how has my
          HRV changed?").

        Do not use this tool when:
        - the user wants two specific, separately-defined ranges compared
          (e.g. "this month vs last month") rather than a single continuous
          slope -> use `compare_metric_periods` instead.
        - the user asks what's typical/normal rather than which direction
          it's moving -> use `get_baseline` instead.
        - the user asks about isolated unusual days rather than an overall
          direction -> use `detect_metric_anomalies` instead.

        Privacy note: this server and its SQLite file are entirely local, but
        the data returned by this tool becomes part of the conversation sent
        to whatever model the calling client is configured with. If that
        model runs in the cloud rather than on your machine, treat this the
        same as pasting the data into a chat with that provider.

        Args:
            metric: One of steps, sleep_hours, resting_heart_rate, weight_kg,
                workout_minutes, mood, water_ml, heart_rate, hrv_ms.
            start_date: First day to include, formatted YYYY-MM-DD.
                Defaults to 30 days before end_date.
            end_date: Last day to include, formatted YYYY-MM-DD. Defaults to today.

        Returns:
            A CalculateTrendResult with "trend" plus "evidence" for the window
            it was fit over. direction is "insufficient_data" below 3 data
            points in range. Even with a direction and slope, a low
            evidence.coverage_ratio or "moderate"/"low" confidence means the
            line is fit through a sparse series — flag that when reporting the
            trend (e.g. "a decline, though the data only covers 60% of days")
            rather than stating the slope as a clean, complete measurement.
        """
        try:
            start, end = resolve_range(start_date, end_date, default_days=30)
        except ValueError as exc:
            raise _tool_error(ERR_INVALID_RANGE, str(exc)) from exc
        series = fetch_metric_series(metric, start, end)
        trend = _trend_stats(series)
        assessment = assess_trend(metric, series, start, end, effect=trend.model_dump())
        evidence = Evidence(**build_evidence(series, start, end))
        return CalculateTrendResult(
            metric=metric,
            range=DateRange(start_date=start.isoformat(), end_date=end.isoformat()),
            trend=trend,
            evidence=evidence,
            **_migrated_claim_fields(assessment, evidence),
        )

    @mcp.tool(
        annotations=ToolAnnotations(
            title="Compare two periods",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        )
    )
    def compare_metric_periods(
        metric: str,
        period_a_start: str,
        period_a_end: str,
        period_b_start: str,
        period_b_end: str,
    ) -> ComparePeriodsResult:
        """
        Compare one metric's average between two date ranges — e.g. "this
        month vs. last month" or "since starting a new medication vs. before."
        The two ranges may be any length and need not be adjacent or equal in
        size; each is summarized with its own baseline first.

        Use this tool when:
        - the user names or implies two distinct date ranges to weigh against
          each other (e.g. "compare my average steps this month to last
          month", "since starting a new medication vs. before").

        Do not use this tool when:
        - there's only one continuous window and the question is about
          direction over time, not two discrete ranges -> use
          `calculate_metric_trend` instead.
        - the user wants "what's normal" for a single window, not a
          before/after comparison -> use `get_baseline` instead.

        Privacy note: this server and its SQLite file are entirely local, but
        the data returned by this tool becomes part of the conversation sent
        to whatever model the calling client is configured with. If that
        model runs in the cloud rather than on your machine, treat this the
        same as pasting the data into a chat with that provider.

        Args:
            metric: One of steps, sleep_hours, resting_heart_rate, weight_kg,
                workout_minutes, mood, water_ml, heart_rate, hrv_ms.
            period_a_start, period_a_end: The "current"/later period, YYYY-MM-DD.
            period_b_start, period_b_end: The period it's compared against, YYYY-MM-DD.

        Returns:
            A ComparePeriodsResult with each period's own baseline stats, plus
            "delta" (period_a mean minus period_b mean) and "pct_change". Read
            claim.decision before reporting the comparison: its "tier"
            (insufficient, suggestive, detectable_not_meaningful, or supported)
            says how strongly it may be stated, and every entry in
            "must_state" has to be mentioned if you report it.
            claim.evidence_a/claim.evidence_b report each period's coverage
            separately — the two periods can have very different coverage
            (e.g. this month is 90% logged, last month only 40%), and that
            asymmetry matters more than either evidence object alone. Both
            delta and pct_change are null if either period has no data at all.
        """
        try:
            start_a, end_a = resolve_range(period_a_start, period_a_end, default_days=0)
            start_b, end_b = resolve_range(period_b_start, period_b_end, default_days=0)
        except ValueError as exc:
            raise _tool_error(ERR_INVALID_RANGE, str(exc)) from exc
        series_a = fetch_metric_series(metric, start_a, end_a)
        series_b = fetch_metric_series(metric, start_b, end_b)
        result = compare_periods(series_a, series_b)
        assessment = assess_window_comparison(
            metric,
            series_a,
            (start_a, end_a),
            series_b,
            (start_b, end_b),
            effect={"delta": result["delta"], "pct_change": result["pct_change"]},
        )
        claim = _claim_evidence_comparative(
            assessment,
            Evidence(**build_evidence(series_a, start_a, end_a)),
            Evidence(**build_evidence(series_b, start_b, end_b)),
        )
        return ComparePeriodsResult(
            metric=metric,
            period_a=DateRange(start_date=start_a.isoformat(), end_date=end_a.isoformat()),
            period_b=DateRange(start_date=start_b.isoformat(), end_date=end_b.isoformat()),
            period_a_stats=BaselineStats(**result["period_a"]),
            period_b_stats=BaselineStats(**result["period_b"]),
            delta=result["delta"],
            pct_change=result["pct_change"],
            claim=claim,
            period_a_evidence=claim.evidence_a,
            period_b_evidence=claim.evidence_b,
            evidence_profile=claim.profile,
            claim_decision=claim.decision,
        )

    @mcp.tool(
        annotations=ToolAnnotations(
            title="Find correlation between two metrics",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        )
    )
    def find_metric_correlation(
        metric_a: str,
        metric_b: str,
        start_date: str | None = None,
        end_date: str | None = None,
        lag_days: int = 0,
    ) -> CorrelationResult:
        """
        Compute the Pearson correlation between two metrics over the same
        window, joined by date. Correlation, not causation: a strong r just
        means the two moved together, not that one caused the other.

        Use this tool when:
        - the user names or implies two *different* metrics and asks whether
          they move together (e.g. "does my sleep affect my mood?", "did my
          HRV change after I increased my workouts?").

        Do not use this tool when:
        - only one metric is in question -> use `get_baseline`,
          `calculate_metric_trend`, or `detect_metric_anomalies` instead,
          depending on what's being asked about that one metric.

        Privacy note: this server and its SQLite file are entirely local, but
        the data returned by this tool becomes part of the conversation sent
        to whatever model the calling client is configured with. If that
        model runs in the cloud rather than on your machine, treat this the
        same as pasting the data into a chat with that provider.

        Args:
            metric_a, metric_b: Any two of steps, sleep_hours,
                resting_heart_rate, weight_kg, workout_minutes, mood, water_ml, heart_rate, hrv_ms.
            start_date: First day to include, formatted YYYY-MM-DD.
                Defaults to 90 days before end_date.
            end_date: Last day to include, formatted YYYY-MM-DD. Defaults to today.
            lag_days: Shift metric_b this many days later before joining — 1
                tests whether metric_a today predicts metric_b tomorrow (e.g.
                "does poor sleep tonight predict lower steps tomorrow?").
                0 (default) compares same-day values.

        Returns:
            A CorrelationResult with "r" (-1 to 1) and "n" (overlapping days
            used); "r" is null with fewer than 4 overlapping days. Read
            claim.decision before reporting "r": its "tier" (insufficient,
            suggestive, detectable_not_meaningful, or supported) says how
            strongly the correlation may be stated, and every entry in
            "must_state" has to be mentioned if you report it. A high "r"
            from a thin paired sample or a gappy series is reflected there,
            not in "r" itself — never judge the strength of a correlation
            from "r" alone.
        """
        try:
            start, end = resolve_range(start_date, end_date, default_days=90)
        except ValueError as exc:
            raise _tool_error(ERR_INVALID_RANGE, str(exc)) from exc
        series_a = fetch_metric_series(metric_a, start, end)
        series_b = fetch_metric_series(metric_b, start, end)
        result = find_correlations(series_a, series_b, lag_days=lag_days)
        assessment = assess_correlation(
            metric_a,
            series_a,
            metric_b,
            series_b,
            start,
            end,
            lag_days=lag_days,
            effect={"r": result["r"], "n": result["n"], "lag_days": lag_days},
        )
        return CorrelationResult(
            metric_a=metric_a,
            metric_b=metric_b,
            **result,
            **_migrated_claim_fields_comparative(
                assessment,
                Evidence(**build_evidence(series_a, start, end)),
                Evidence(**build_evidence(series_b, start, end)),
            ),
        )
