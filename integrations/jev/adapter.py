"""Pure, network-free helpers for the P0 Jev contract.

Nothing in this module makes an HTTP request, imports an HTTP or Jev SDK
library, or has any way to construct or alter a ClaimEvidence. The API
client is a separate, later (P1) patch, added only once the tests in this
package's test suite — in particular that an insufficient claim cannot be
upgraded — are in place and green.
"""

from __future__ import annotations

from typing import Any

import schemas

from .schemas import ALLOWED_ANOMALY_FIELDS


def build_anomaly_question_input(result: schemas.DetectAnomaliesResult, change_percent: float) -> dict[str, Any]:
    """Build the sanitized, allowlisted payload for an anomaly-classification
    question from an already-computed DetectAnomaliesResult.

    `change_percent` is supplied by the caller rather than derived here,
    since DetectAnomaliesResult does not itself carry a single scalar change
    (it carries a list of per-day anomaly points); callers pick which
    anomaly they're asking about.

    Does not read or return anything from `result` beyond what's on
    ALLOWED_ANOMALY_FIELDS, and does not mutate `result`.

    Raises ValueError if the payload this function would build contains
    anything outside ALLOWED_ANOMALY_FIELDS. This is a hard stop rather than
    a silent filter, so that a field added here later without being added to
    the allowlist fails loudly instead of leaking.
    """
    evidence = result.claim.evidence
    payload: dict[str, Any] = {
        "metric": result.metric,
        "change_percent": change_percent,
        "baseline_days": evidence.expected_days,
        "observed_days": evidence.observed_days,
        "coverage": evidence.coverage_ratio,
        "claim_tier": result.claim.decision.tier,
    }
    unexpected = set(payload) - ALLOWED_ANOMALY_FIELDS
    if unexpected:
        raise ValueError(f"jev anomaly payload fields outside the allowlist: {sorted(unexpected)}")
    return payload
