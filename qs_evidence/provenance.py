"""Provenance evaluator (Dimension.PROVENANCE).

Input is the source summary the daily_metrics projection already records for a window (see
logic.projected_source_summary): which sources supplied the resolved values, and on how many days several
sources disagreed and none was ranked in source_priority, so the winner was an arbitrary tie-break.

A window built from a source that changed mid-way, or from arbitrary tie-breaks, is a degraded series, not an
unusable one: it caps the claim (WEAK) and names why. An unreadable summary is NOT_ASSESSED, never adequate, so
a missing provenance read can only lower a decision. Nothing here reads confidence, n_eff or Jev output.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from .models import Dimension, DimensionResult, Status

UNRANKED_CONFLICT = "unranked_source_conflict_in_window"
SOURCE_CHANGED_WITHIN = "source_changed_within_window"
SOURCE_CHANGED_BETWEEN = "source_changed_between_windows"
UNAVAILABLE = "provenance_unavailable"


@dataclass(frozen=True)
class SourceSummary:
    sources: tuple[str, ...]
    unranked_conflict_days: int

    @classmethod
    def from_mapping(cls, summary: dict) -> SourceSummary:
        return cls(tuple(sorted(summary["sources"])), int(summary["unranked_conflict_days"]))


def _unavailable() -> DimensionResult:
    return DimensionResult(dimension=Dimension.PROVENANCE, status=Status.NOT_ASSESSED, reason_codes=[UNAVAILABLE])


def _reasons(s: SourceSummary) -> list[str]:
    reasons: list[str] = []
    if s.unranked_conflict_days > 0:
        reasons.append(UNRANKED_CONFLICT)
    if len(s.sources) > 1:
        reasons.append(SOURCE_CHANGED_WITHIN)
    return reasons


def evaluate_provenance(windows: dict[str, SourceSummary | None], *, same_metric: bool = False) -> DimensionResult:
    """One result for every window an analysis rests on. Any unreadable window makes the dimension NOT_ASSESSED
    unless another window already shows a problem (a problem is stronger information than a gap).

    `same_metric` is True only when the windows are the same metric (a period comparison): then differing source
    sets between windows is itself a reason, because the comparison would partly measure the source change.
    Different metrics (a correlation) legitimately come from different sources."""
    if not windows:
        return _unavailable()
    reasons: list[str] = []
    details: dict = {"windows": {}}
    missing = False
    for name, summary in windows.items():
        if summary is None:
            missing = True
            details["windows"][name] = {"status": Status.NOT_ASSESSED.value}
            continue
        found = _reasons(summary)
        reasons += [c for c in found if c not in reasons]
        details["windows"][name] = {
            "status": (Status.WEAK if found else Status.ADEQUATE).value,
            "sources": list(summary.sources),
            "unranked_conflict_days": summary.unranked_conflict_days,
        }
    if same_metric:
        known = [s.sources for s in windows.values() if s is not None and s.sources]
        if len(set(known)) > 1:
            reasons.append(SOURCE_CHANGED_BETWEEN)
    if reasons:
        return DimensionResult(
            dimension=Dimension.PROVENANCE, status=Status.WEAK, reason_codes=reasons, details=details
        )
    if missing:
        result = _unavailable()
        result.details = details
        return result
    return DimensionResult(dimension=Dimension.PROVENANCE, status=Status.ADEQUATE, details=details)


def evaluate_single(summary: SourceSummary | None) -> DimensionResult:
    return evaluate_provenance({"window": summary})


def clean_windows(names: Sequence[str]) -> dict[str, SourceSummary | None]:
    """Test/diagnostic helper: windows with one consistent source and no conflicts."""
    return {n: SourceSummary(("src",), 0) for n in names}
