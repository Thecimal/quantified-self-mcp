"""
MCP input/output schemas
=========================

Every Pydantic model used as a tool/resource return type (or nested
inside one) in server.py, moved here verbatim — no field, default,
validator, or docstring changed. FastMCP derives each tool's JSON
output schema from these via the return-type annotation; see
server.py for where each one is actually used.
"""

from pydantic import BaseModel, Field, model_validator

from qs_evidence import ClaimDecision, ClaimTier, EvidenceProfile, combine_decisions, validate_claim_decision


class DailyMetricsRow(BaseModel):
    date: str
    steps: int | None = None
    sleep_hours: float | None = None
    resting_heart_rate: int | None = None
    weight_kg: float | None = None
    workout_minutes: int | None = None
    mood: int | None = None
    water_ml: int | None = None
    heart_rate: int | None = None
    hrv_ms: float | None = None


class DateRange(BaseModel):
    start_date: str
    end_date: str


class Gap(BaseModel):
    start: str
    end: str
    days: int


class Evidence(BaseModel):
    """Coverage/quality of the data a single-metric analytical result is
    based on. See evidence.build_evidence for how each field is computed.
"""

    requested_start: str
    requested_end: str
    observed_start: str | None = None
    observed_end: str | None = None
    expected_days: int
    observed_days: int
    coverage_ratio: float
    missing_days: int
    measurement_count: int
    gaps: list[Gap]
    freshness_days: int | None = None
    recent_gap_days: int
    confidence: str


class MetricStats(BaseModel):
    avg: float | None = None
    min: float | None = None
    max: float | None = None


class HealthDataSummary(BaseModel):
    days_with_data: int
    steps: MetricStats
    sleep_hours: MetricStats
    resting_heart_rate: MetricStats
    weight_kg: MetricStats
    workout_minutes: MetricStats
    mood: MetricStats
    water_ml: MetricStats
    heart_rate: MetricStats
    hrv_ms: MetricStats


class CoverageSummary(BaseModel):
    """Multi-metric coverage for a Layer-1 result spanning several metrics
    at once. See evidence.build_coverage_summary for how each field is
    computed; unlike Evidence (one metric, gaps/freshness/recent_gap),
    this reports a coverage_percent per metric side by side.
    """

    period: str
    days_expected: int
    days_with_data: int
    coverage_percent: float
    missing_days: int
    metrics: dict[str, float]
    confidence: str


class ReadHealthDataResult(BaseModel):
    range: DateRange
    rows: list[DailyMetricsRow]
    truncated: bool
    summary: HealthDataSummary
    coverage: CoverageSummary


class ExportCsvResult(BaseModel):
    path: str
    rows_exported: int
    range: DateRange


class LogDailyMetricResult(BaseModel):
    logged: dict[str, int | float]
    row: DailyMetricsRow


class ClearMetricResult(BaseModel):
    cleared: str
    row: DailyMetricsRow | None = None
    note: str | None = None


class MeasurementRow(BaseModel):
    id: int
    timestamp: str
    metric: str
    value: float | None  # null for a metric listed in HEALTH_PRIVATE_FIELDS
    unit: str | None = None
    source: str | None = None
    source_type: str | None = None
    created_at: str


class LogMeasurementResult(BaseModel):
    measurement: MeasurementRow


class ReadMeasurementsResult(BaseModel):
    measurements: list[MeasurementRow]
    count: int


class AggregateMeasurementsResult(BaseModel):
    date: str
    aggregated: dict[str, float]
    row: DailyMetricsRow


class SourceBreakdown(BaseModel):
    source: str | None = None
    value: float
    n: int
    latest_timestamp: str


class GetMetricProvenanceResult(BaseModel):
    metric: str
    date: str
    sources: list[SourceBreakdown]
    conflict: bool


# --- Layer 2 (analytics) / Layer 3 (personal intelligence) output models --
#
# These wrap the pure functions in analytics.py the same way the models
# above wrap logic.py: FastMCP derives a JSON schema from the return type,
# so a client gets structured_content it can consume directly rather than
# re-parsing a free-form string.


class MetricSeriesPoint(BaseModel):
    date: str
    value: float


class DataHealthFreshness(BaseModel):
    latest_data: str | None = None
    age_days: int | None = Field(
        default=None, description="Days from the latest observation to the end of the requested window."
    )
    dataset_days_behind: int | None = Field(
        default=None, description="Days from the dataset's latest data (any metric) to today."
    )


class DataHealthCoverage(BaseModel):
    start: str | None = None
    end: str | None = None
    requested_start: str
    requested_end: str


class DataHealthCompleteness(BaseModel):
    expected_days: int
    observed_days: int
    coverage_ratio: float
    missing_days: int


class DataHealthImport(BaseModel):
    importer: str
    status: str
    finished_at: str | None = None
    source_file: str | None = None


class DataHealth(BaseModel):
    """Data-quality state behind one result (not a medical or statistical confidence score). status is one of
    VALID, VALID_WITH_GAPS, INSUFFICIENT_DATA, STALE, IMPORT_INCOMPLETE; reasons lists every condition that
    applies. See data_health.compose_data_health for how it is decided."""

    status: str
    reasons: list[str]
    freshness: DataHealthFreshness
    coverage: DataHealthCoverage
    completeness: DataHealthCompleteness
    gaps: list[Gap]
    observations: int = Field(description="Number of daily values the result is computed from.")
    last_import: DataHealthImport | None = None
    last_successful_import: str | None = None


class GetMetricHistoryResult(BaseModel):
    metric: str
    range: DateRange
    points: list[MetricSeriesPoint]
    evidence: Evidence
    data_health: DataHealth | None = Field(
        default=None, description="Whether this history is complete, current and trustworthy; see DataHealth."
    )


class ClaimDecisionOut(BaseModel):
    tier: str
    permitted_phrasing_class: str
    must_state: list[str]
    template: str


class ClaimEvidence(BaseModel):
    """Canonical envelope for one assessed claim: the coverage evidence it rests on, the per-dimension
    EvidenceProfile, and the resulting ClaimDecision. It is the single claim representation on every
    result that carries a claim; there are no parallel flat profile/decision fields."""

    evidence: Evidence = Field(description="Descriptive data coverage behind this claim.")
    profile: EvidenceProfile = Field(
        description=(
            "Per-dimension evidence quality behind this claim (sample, temporal, missingness, ...). "
            "Dimensions without an evaluator yet are 'not_assessed' and cap the claim, "
            "never count as adequate."
        ),
    )
    decision: ClaimDecisionOut = Field(
        description=(
            "How strongly this claim may be stated. tier is insufficient, suggestive, "
            "detectable_not_meaningful or supported; every entry in must_state has to be "
            "mentioned when reporting it."
        ),
    )

    @model_validator(mode="after")
    def _decision_matches_profile(self):
        validate_claim_decision(self.profile, self.decision.model_dump())
        return self


class ClaimEvidenceComparative(BaseModel):
    """Canonical envelope for one assessed claim resting on two source coverages (e.g. two metrics, two
    periods) rather than one — see ClaimEvidence for the single-window form. There is still exactly one
    profile and one decision: per-source facts that matter to the assessment (paired sample size, each
    source's missingness, ...) live inside profile's dimension details, not as a second decision surface."""

    evidence_a: Evidence = Field(description="Descriptive data coverage behind the first source.")
    evidence_b: Evidence = Field(description="Descriptive data coverage behind the second source.")
    profile: EvidenceProfile = Field(
        description=(
            "Per-dimension evidence quality behind this claim (sample, temporal, missingness, ...), "
            "assessed jointly across both sources where relevant. Dimensions without an evaluator yet "
            "are 'not_assessed' and cap the claim, never count as adequate."
        ),
    )
    decision: ClaimDecisionOut = Field(
        description=(
            "How strongly this claim may be stated. tier is insufficient, suggestive, "
            "detectable_not_meaningful or supported; every entry in must_state has to be "
            "mentioned when reporting it."
        ),
    )

    @model_validator(mode="after")
    def _decision_matches_profile(self):
        validate_claim_decision(self.profile, self.decision.model_dump())
        return self


class BaselineStats(BaseModel):
    mean: float | None = None
    median: float | None = None
    stdev: float | None = None
    n: int


class GetBaselineResult(BaseModel):
    metric: str
    range: DateRange
    baseline: BaselineStats
    claim: ClaimEvidence = Field(
        description="Evidence and decision for the \"what's normal\" claim; read claim.decision before reporting it."
    )
    evidence: Evidence = Field(
        deprecated=True,
        description="DEPRECATED: identical to claim.evidence. Use claim.decision, not evidence.confidence.",
    )
    data_health: DataHealth | None = Field(
        default=None, description="Whether the data behind this baseline is complete, current and trustworthy."
    )


class AnomalyPoint(BaseModel):
    date: str
    value: float
    modified_z_score: float
    direction: str


class DetectAnomaliesResult(BaseModel):
    metric: str
    range: DateRange
    threshold: float
    anomalies: list[AnomalyPoint]
    claim: ClaimEvidence = Field(
        description="Evidence and decision for the anomaly claim; read claim.decision before reporting it."
    )
    evidence: Evidence = Field(
        deprecated=True,
        description="DEPRECATED: identical to claim.evidence. Use claim.decision, not evidence.confidence.",
    )


class TrendStats(BaseModel):
    direction: str
    slope_per_day: float | None = None
    r_squared: float | None = None
    n: int
    span_days: int | None = None


class CalculateTrendResult(BaseModel):
    metric: str
    range: DateRange
    trend: TrendStats
    claim: ClaimEvidence = Field(
        description="Evidence and decision for the trend claim; read claim.decision before reporting it."
    )
    evidence: Evidence = Field(
        deprecated=True,
        description="DEPRECATED: identical to claim.evidence. Use claim.decision, not evidence.confidence.",
    )


class ComparePeriodsResult(BaseModel):
    metric: str
    period_a: DateRange
    period_b: DateRange
    period_a_stats: BaselineStats
    period_b_stats: BaselineStats
    delta: float | None = None
    pct_change: float | None = None
    claim: ClaimEvidenceComparative = Field(
        description="Evidence and decision for the period comparison claim; read claim.decision before reporting it."
    )
    period_a_evidence: Evidence = Field(
        deprecated=True,
        description="DEPRECATED: identical to claim.evidence_a. Use claim.decision, not a standalone confidence field.",
    )
    period_b_evidence: Evidence = Field(
        deprecated=True,
        description="DEPRECATED: identical to claim.evidence_b. Use claim.decision, not a standalone confidence field.",
    )


class CorrelationResult(BaseModel):
    metric_a: str
    metric_b: str
    lag_days: int
    r: float | None = None
    n: int
    note: str | None = None
    claim: ClaimEvidenceComparative = Field(
        description="Evidence and decision for the correlation claim; read claim.decision before reporting it."
    )
    evidence_a: Evidence = Field(
        deprecated=True,
        description="DEPRECATED: identical to claim.evidence_a. Use claim.decision, not a standalone confidence field.",
    )
    evidence_b: Evidence = Field(
        deprecated=True,
        description="DEPRECATED: identical to claim.evidence_b. Use claim.decision, not a standalone confidence field.",
    )


class ChangeNote(BaseModel):
    metric: str
    kind: str  # "shift" (period-over-period) | "anomaly" | "trend"
    detail: str
    claim: ClaimEvidence = Field(
        description="Evidence and decision for this change note's claim; read claim.decision before reporting it."
    )
    evidence: Evidence = Field(
        deprecated=True,
        description="DEPRECATED: identical to claim.evidence. Use claim.decision, not evidence.confidence.",
    )


class GetRecentChangesResult(BaseModel):
    recent_range: DateRange
    baseline_range: DateRange
    changes: list[ChangeNote]


class WorkoutSessionRow(BaseModel):
    id: int
    date: str
    activity_type: str
    start_time: str | None = None
    duration_minutes: int
    intensity: str | None = None
    avg_heart_rate: int | None = None
    max_heart_rate: int | None = None
    source: str | None = None
    notes: str | None = None
    created_at: str


class LogWorkoutSessionResult(BaseModel):
    session: WorkoutSessionRow


class ReadWorkoutSessionsResult(BaseModel):
    sessions: list[WorkoutSessionRow]
    count: int


class ExplainMetricChangeResult(BaseModel):
    metric: str
    date: str
    value: float | None = None
    baseline_range: DateRange
    baseline: BaselineStats
    is_anomaly: bool
    modified_z_score: float | None = None
    trend: TrendStats
    correlated_metrics: list[CorrelationResult]
    sessions: list[WorkoutSessionRow] = []
    narrative_facts: list[str]
    baseline_claim: ClaimEvidence = Field(
        description=(
            "Evidence and decision for the 90-day baseline statistics reported in \"baseline\" (and quoted in "
            "narrative_facts). Read baseline_claim.decision before stating what is typical for this metric; "
            "it is one of the components overall_decision is the weakest of."
        )
    )
    headline_claim: ClaimEvidence = Field(
        description=(
            "Evidence and decision for the headline claim: this day's value against its 90-day "
            "baseline. Read headline_claim.decision before reporting it."
        ),
    )
    trend_claim: ClaimEvidence = Field(
        description=(
            "Evidence and decision for the 30-day trend leading into this day, assessed separately "
            "from the headline claim. Read trend_claim.decision before reporting it."
        ),
    )
    conflicting_days: int = 0
    overall_decision: ClaimDecisionOut = Field(
        description=(
            "The single decision governing this whole result: weakest-of-N over the headline claim "
            "(headline_claim.decision), the trend (trend_claim.decision), and every surfaced "
            "correlation's own decision. If any component is insufficient/suggestive, the composite "
            "is too — report overall_decision.tier and must_state rather than treating the headline "
            "claim as though the trend and correlations couldn't drag it down."
        ),
    )

    @model_validator(mode="after")
    def _overall_is_weakest_of_components(self):
        components = [
            self.baseline_claim.decision,
            self.headline_claim.decision,
            self.trend_claim.decision,
            *(c.claim.decision for c in self.correlated_metrics),
        ]
        expected = combine_decisions(
            [
                ClaimDecision(
                    tier=ClaimTier(d.tier),
                    limiting_factors=list(d.must_state),
                    permitted_phrasing_class=d.permitted_phrasing_class,
                )
                for d in components
            ]
        ).to_mcp()
        if self.overall_decision.model_dump() != expected:
            raise ValueError(
                "overall_decision is not the weakest-of-N of its component claims: "
                f"expected {expected!r}, got {self.overall_decision.model_dump()!r}"
            )
        return self


class MetricDefinition(BaseModel):
    name: str
    min: float
    max: float
    label: str
    private: bool  # mirrors PRIVATE_FIELDS at the time this is read
