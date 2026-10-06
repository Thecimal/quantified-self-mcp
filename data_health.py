"""
data_health.py
==============
Composes one deterministic data-quality state for an analytical result from
two things the server already computes: the per-metric evidence for the
requested window (evidence.build_evidence) and the dataset-level status
(logic.compute_data_status). This is a statement about the data behind an
answer, not a medical or statistical confidence score.

Framework-free (standard library only), mirroring evidence.py: plain dicts
in, plain dict out. server.py wraps the result in schemas.DataHealth.

Status, checked in this order (every reason that applies is listed, but the
status is the first match):
  IMPORT_INCOMPLETE  the most recent import failed or never finished
  INSUFFICIENT_DATA  no observations in the window, or the claim assessment
                     already rates the data tier "insufficient"
  STALE              the latest observation trails the end of the window by
                     more than stale_after_days
  VALID_WITH_GAPS    days inside the window have no observation
  VALID              none of the above
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

DEFAULT_STALE_AFTER_DAYS = 2

STATUSES = ("VALID", "VALID_WITH_GAPS", "INSUFFICIENT_DATA", "STALE", "IMPORT_INCOMPLETE")

# Most severe first: used to pick the weakest window when a result rests on several.
SEVERITY_ORDER = ("IMPORT_INCOMPLETE", "INSUFFICIENT_DATA", "STALE", "VALID_WITH_GAPS", "VALID")


def compose_data_health(
    evidence: dict[str, Any],
    dataset: dict[str, Any] | None = None,
    *,
    decision_tier: str | None = None,
    must_state: Sequence[str] = (),
    stale_after_days: int = DEFAULT_STALE_AFTER_DAYS,
) -> dict[str, Any]:
    """Build the data-health dict for one result.

    `evidence` is an evidence.build_evidence dict (or its model_dump).
    `dataset` is a logic.compute_data_status dict, or None if it could not be
    read (reported as the reason dataset_status_unavailable; the status is
    then decided from the evidence alone). `decision_tier` and `must_state`
    come from the claim decision when the result carries one.
    """
    latest_import = dataset.get("latest_import") if dataset else None

    reasons: list[str] = []
    if dataset is not None and dataset["status"] == "IMPORT_FAILED":
        reasons.append(dataset["reason"] or "import_failed")
    if evidence["observed_days"] == 0:
        reasons.append("no_observations_in_window")
    if decision_tier == "insufficient":
        reasons.extend(r for r in must_state if r not in reasons)
        if not must_state:
            reasons.append("claim_tier_insufficient")
    freshness_days = evidence["freshness_days"]
    if freshness_days is not None and freshness_days > stale_after_days:
        reasons.append("latest_observation_older_than_threshold")
    if evidence["gaps"]:
        reasons.append("gaps_in_window")
    if dataset is None:
        reasons.append("dataset_status_unavailable")

    if dataset is not None and dataset["status"] == "IMPORT_FAILED":
        status = "IMPORT_INCOMPLETE"
    elif evidence["observed_days"] == 0 or decision_tier == "insufficient":
        status = "INSUFFICIENT_DATA"
    elif freshness_days is not None and freshness_days > stale_after_days:
        status = "STALE"
    elif evidence["gaps"]:
        status = "VALID_WITH_GAPS"
    else:
        status = "VALID"

    return {
        "status": status,
        "reasons": reasons,
        "freshness": {
            "latest_data": evidence["observed_end"],
            "age_days": freshness_days,
            "dataset_days_behind": dataset["days_behind"] if dataset else None,
        },
        "coverage": {
            "start": evidence["observed_start"],
            "end": evidence["observed_end"],
            "requested_start": evidence["requested_start"],
            "requested_end": evidence["requested_end"],
        },
        "completeness": {
            "expected_days": evidence["expected_days"],
            "observed_days": evidence["observed_days"],
            "coverage_ratio": evidence["coverage_ratio"],
            "missing_days": evidence["missing_days"],
        },
        "gaps": list(evidence["gaps"]),
        "observations": evidence["measurement_count"],
        "last_import": (
            {
                "importer": latest_import["importer"],
                "status": latest_import["status"],
                "finished_at": latest_import["finished_at"],
                "source_file": latest_import["source_file"],
            }
            if latest_import
            else None
        ),
        "last_successful_import": dataset["last_successful_import"] if dataset else None,
    }


def merge_data_health(healths: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Combine the data health of several windows (two periods, two metrics, one metric over two windows)
    into one result: the weakest window's dict, with every window's reasons listed (that window's first,
    then the others', without repeats). Raises ValueError for an empty sequence."""
    if not healths:
        raise ValueError("merge_data_health needs at least one data-health dict")
    weakest = min(healths, key=lambda health: SEVERITY_ORDER.index(health["status"]))
    reasons = list(weakest["reasons"])
    for health in healths:
        reasons.extend(reason for reason in health["reasons"] if reason not in reasons)
    return {**weakest, "reasons": reasons}
