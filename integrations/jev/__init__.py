"""Optional Jev decision-classification integration.

Disabled by default and, as of this P0 patch, has no enabled state at all:
there is no client, no API key handling, and no code path that makes a
network call. This patch defines only the contract — JevInterpretation and
the sanitized input it would be built from — so that the authority and
privacy boundaries can be tested before any client exists.
"""

from .adapter import build_anomaly_question_input
from .schemas import ALLOWED_ANOMALY_FIELDS, JevInterpretation

__all__ = ["ALLOWED_ANOMALY_FIELDS", "JevInterpretation", "build_anomaly_question_input"]
