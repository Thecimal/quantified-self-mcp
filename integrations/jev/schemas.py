"""Contract types for the optional Jev decision-classification integration.

P0 scope only: the data contract. No network client, no Jev SDK dependency,
no change to any existing analytics result or claim. See the architecture
note tracked for this integration for the full plan; the client (P1) is a
separate, later patch gated on these contract tests passing.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class JevInterpretation:
    """An optional, non-authoritative interpretation of an already-computed
    analytics result.

    This is never a substitute for ClaimEvidence and must never be merged
    into one: it carries no tier, no must_state, no permitted_phrasing_class,
    and nothing else that could change how strongly a claim may be stated.
    `confidence` here is Jev's certainty in its own classification — it says
    nothing about whether the underlying health data was sufficient, and
    must never be read as a statistical confidence value.
    """

    category: str
    probabilities: dict[str, float]
    confidence: float
    model: str


# Fields permitted to leave the process as part of an anomaly-classification
# question. This is a hard allowlist, not a denylist: anything not named here
# — user identifiers, raw health records, dates/timestamps beyond what's
# listed, free-text notes — must never be sent to Jev. A new field added to
# an analytics result in the future is excluded by default, not included by
# default; see integrations.jev.adapter.build_anomaly_question_input, which
# enforces this at construction time rather than trusting callers to respect it.
ALLOWED_ANOMALY_FIELDS: frozenset[str] = frozenset(
    {
        "metric",
        "change_percent",
        "baseline_days",
        "observed_days",
        "coverage",
        "claim_tier",
    }
)
