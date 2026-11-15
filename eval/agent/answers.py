"""Deterministic scoring of the agent's final answer against the tool results it was given.

Two layers, filled in only for scenarios that carry an `answer` spec (every other scenario stays NOT_SCORED):

- grounding: the answer states the numbers, units, dates and metric the tool results contain, and does not
  turn missing data into a value (a day with no steps recorded is not "0 steps").
- interpretation: the answer respects what the evidence allows. The expectations come from the tool results
  themselves: `data_health.status` (STALE, MIXED_SOURCES, IMPORT_INCOMPLETE, INSUFFICIENT_DATA) and the claim tier
  of `claim.decision` (insufficient, suggestive, supported). An answer must disclose a stale, conflicting,
  failed-import or insufficient dataset, must hedge a suggestive claim, must not use certainty language for
  anything below supported, and must not state a cause for an association.

A layer is scored only when at least one of its checks applied to the trace, so an answer is never "passed" by
having nothing checked. Every failure names the violated expectation: {"layer", "code", "detail"}.

The checks are phrase-based. They catch an answer that leaves a mandatory caveat out or states what the evidence
does not support; they do not judge style, and an answer that conveys a caveat in words none of the patterns
recognize fails (see "Limits" in eval/agent/README.md). No model is involved, so results are reproducible offline.

Spec (`answer:` in a scenario, all keys optional):

    values:   [{tool, path, unit?, code?}]  a number from the result at `path` must appear in the answer, followed
                                            by the regex `unit` when given
    dates:    ["{today}" | {tool, path}]     a date that must appear (ISO, "Nov 3" or "3 November")
    require:  [{code, layer, any: [regex]}]  at least one of the regexes must match
    forbid:   [{code, layer, any: [regex]}]  none of the regexes may match an affirmative sentence
    forbid_causal: true                      apply the causal-claim check even when no result is a correlation
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from eval.agent.loop import Trace

INTERPRETATION, GROUNDING = "interpretation", "grounding"
SPEC_KEYS = frozenset({"values", "dates", "require", "forbid", "forbid_causal"})
LAYERS_SCORED = (INTERPRETATION, GROUNDING)

_NEGATION = re.compile(r"\b(not|no|never|without|cannot|neither|nor)\b|n't|\bnothing\b", re.I)
_LOCAL = 20  # characters before a match inspected for a negation, where the whole sentence would be too broad


def _rx(*patterns: str) -> re.Pattern[str]:
    return re.compile("|".join(patterns), re.I)


# What a caveat sounds like. Each is deliberately a little generous: the point is to notice an answer that says
# nothing of the kind, not to demand one wording.
INSUFFICIENT = _rx(
    r"not enough",
    r"insufficient",
    r"too (few|little|sparse|short)",
    r"(isn't|is not|aren't|are not|wasn't|was not) (enough|sufficient|reliable|a reliable)",
    r"(can't|cannot|can not|unable to|couldn't) (\w+ ){0,3}(say|tell|conclude|determine|establish|give|calculate|"
    r"draw|reliably)",
    r"no (\w+ ){0,3}(data|records?|readings?|measurements?|entries|observations)",
    r"only \d+ (readings?|measurements?|observations?|days?|data points?|entries)",
    r"(very )?(limited|sparse|thin) data",
    r"not (a )?(reliable|meaningful|trustworthy)",
)
HEDGE = _rx(
    r"suggest",
    r"\bhints?\b",
    r"\b(may|might|could)\b",
    r"possibl",
    r"tentativ",
    r"preliminar",
    r"not (conclusive|definitive|proof|proven|certain|established)",
    r"\bappears?\b",
    r"\bseems?\b",
    r"\blikely\b",
    r"association",
    r"worth (watching|monitoring|keeping an eye)",
)
CERTAINTY = _rx(
    r"\b(definitely|certainly|undoubtedly|conclusively|unquestionably|obviously)\b",
    r"\bproves?\b",
    r"\bconfirms?\b",
    r"\bclearly (shows?|drives?|means?|indicates?|demonstrates?)\b",
    r"\bwithout (a )?doubt\b",
    r"\bfor (sure|certain)\b",
)
CAUSAL = _rx(
    r"\b(because|caused?|causing|causes)\b",
    r"\b(due|thanks) to\b",
    r"\bleads? to\b",
    r"\bled to\b",
    r"\bresults? in\b",
    r"\bresulted in\b",
    r"\bdrives?\b",
    r"\bdriving\b",
    r"\bis (the )?(reason|why)\b",
    r"\bexplains? why\b",
    r"\bresponsible for\b",
    r"\bboosts?\b",
    r"\bimproves?\b",
    r"\bresult of\b",
)
STALE = _rx(
    r"\bstale\b",
    r"out of date",
    r"outdated",
    r"\bold(er)?\b",
    r"not (been )?(updated|synced|refreshed|recorded|logged|current)",
    r"\b\d+ days? (old|ago|behind|out of date)",
    r"last (updated|synced|recorded|logged|measurement|reading|data)",
    r"(latest|most recent) (data|reading|measurement|entry|record)\b[^.]{0,40}\b(is|was|from|dates?|on)\b",
    r"(data|readings?) (stops?|ends?|stopped|ended) ",
    r"as of ",
)
CONFLICT = _rx(
    r"(two|multiple|different|several|both) sources",
    r"sources? (disagree|conflict|differ|don't match|do not match|report different)",
    r"\bdisagree",
    r"\bconflict",
    r"\bdiscrepanc",
    r"inconsisten",
    r"\bmixed sources\b",
)
IMPORT_FAILED = _rx(
    r"import[^.]{0,40}(fail|did not (complete|finish|succeed)|didn't (complete|finish|succeed)|incomplete|error)",
    r"(fail|incomplete|error)[^.]{0,40}import",
    r"(latest|last|most recent) import[^.]{0,30}(fail|incomplete)",
)
CLEAN_CLAIMS = _rx(
    r"up[- ]to[- ]date",
    r"fully (current|up[- ]to[- ]date|complete)",
    r"data is (complete|current|fresh|up to date)",
    r"complete (data|record|history)",
    r"no (missing|gaps?|issues|problems|concerns|caveats?)\b",
    r"nothing (is )?missing",
)
ZERO_VALUE = _rx(
    r"\b(0|zero|none)\s+(steps|hours|minutes|ms|bpm|kg|calories|readings?|measurements?|workouts?)\b",
    r"\b(walked|took|logged|recorded|slept|did|got|had)\s+(0|zero|no)\s+(steps|hours|minutes|workouts?)\b",
    r"\b(was|were|is|are|came to|comes to|totalled|totaled)\s+(0|zero)\b",
    r"\bzero\b",
    r"(?<![\d.,])0\s*(steps|hours|ms|bpm|kg)\b",
)


@dataclass
class AnswerScore:
    interpretation: bool | None
    grounding: bool | None
    failures: list[dict[str, str]] = field(default_factory=list)


def validate_spec(spec: Any) -> list[str]:
    """Structural problems of one scenario's `answer` spec (empty list when it is well-formed)."""
    if not isinstance(spec, dict):
        return ["answer must be a mapping"]
    problems = [f"unknown answer key {key!r}" for key in sorted(set(spec) - SPEC_KEYS)]
    for key in ("values", "dates", "require", "forbid"):
        if key in spec and not isinstance(spec[key], list):
            problems.append(f"answer.{key} must be a list")
    for item in spec.get("values", []) if isinstance(spec.get("values"), list) else []:
        if not isinstance(item, dict) or not {"tool", "path"} <= set(item):
            problems.append(f"answer.values entry needs tool and path: {item!r}")
    for key in ("require", "forbid"):
        for item in spec.get(key, []) if isinstance(spec.get(key), list) else []:
            if not isinstance(item, dict) or not {"code", "layer", "any"} <= set(item):
                problems.append(f"answer.{key} entry needs code, layer and any: {item!r}")
            elif item["layer"] not in LAYERS_SCORED:
                problems.append(f"answer.{key} {item['code']}: layer must be one of {LAYERS_SCORED}")
            elif not isinstance(item["any"], list) or not item["any"]:
                problems.append(f"answer.{key} {item['code']}: any must be a non-empty list of regexes")
            else:
                for pattern in item["any"]:
                    try:
                        re.compile(pattern)
                    except re.error as exc:
                        problems.append(f"answer.{key} {item['code']}: bad regex {pattern!r}: {exc}")
    return problems


# ---- helpers -------------------------------------------------------------------------------------------------


def _dig(obj: Any, path: str) -> Any:
    for part in path.split("."):
        if isinstance(obj, dict):
            obj = obj.get(part)
        elif isinstance(obj, list) and part.isdigit() and int(part) < len(obj):
            obj = obj[int(part)]
        else:
            return None
    return obj


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _sentences(text: str) -> list[str]:
    return [s for s in re.split(r"(?<=[.!?])\s+|\n+", text) if s.strip()]


def _affirmative(patterns: re.Pattern[str], text: str, *, local: bool = False) -> str | None:
    """The first sentence in which `patterns` matches without a negation, else None. A causal word in "that does
    not mean it causes anything" is a disclaimer, not a claim. With local=True only the few words before the match
    count ("didn't walk at all - zero steps" is still a zero; "that is not zero" is not)."""
    for sentence in _sentences(text):
        for match in patterns.finditer(sentence):
            context = sentence[max(0, match.start() - _LOCAL) : match.start()] if local else sentence
            if not _NEGATION.search(context):
                return sentence.strip()
    return None


def _any(patterns: re.Pattern[str], text: str) -> bool:
    return patterns.search(text) is not None


def _number_tokens(text: str) -> list[tuple[float, int, int, int]]:
    """(value, decimals, start, end) of every number in the text; "7,000" and "7000" are the same number."""
    out = []
    for match in re.finditer(r"(?<![\w.])-?\d[\d,]*(?:\.\d+)?", text):
        raw = match.group().replace(",", "")
        try:
            value = float(raw)
        except ValueError:
            continue
        decimals = len(raw.split(".")[1]) if "." in raw else 0
        out.append((value, decimals, match.start(), match.end()))
    return out


def _states_number(text: str, expected: float, unit: str | None) -> tuple[bool, bool]:
    """(number found, number found with the unit after it). The answer's own rounding sets the tolerance."""
    found = False
    for value, decimals, _, end in _number_tokens(text):
        if abs(value - expected) <= 0.5 * 10**-decimals + 1e-9:
            found = True
            if unit is None or re.search(unit, text[end : end + 24], re.I):
                return True, True
    return found, False


def _date_forms(day: date) -> list[str]:
    month_long, month_short = day.strftime("%B"), day.strftime("%b")
    return [
        day.isoformat(),
        f"{month_long} {day.day}",
        f"{month_short} {day.day}",
        f"{month_short}. {day.day}",
        f"{day.day} {month_long}",
        f"{day.day} {month_short}",
    ]


def _states_date(text: str, day: date) -> bool:
    low = text.lower()
    return any(re.search(rf"(?<!\d){re.escape(form.lower())}(?!\d)", low) for form in _date_forms(day))


class _Checker:
    def __init__(self, answer: str) -> None:
        self.answer = answer
        self.failures: list[dict[str, str]] = []
        self.ran: set[str] = set()

    def ran_layer(self, layer: str) -> None:
        self.ran.add(layer)

    def fail(self, layer: str, code: str, detail: str) -> None:
        if not any(f["code"] == code and f["detail"] == detail for f in self.failures):
            self.failures.append({"layer": layer, "code": code, "detail": detail})


def _excerpt(sentence: str, limit: int = 110) -> str:
    sentence = " ".join(sentence.split())
    return sentence if len(sentence) <= limit else sentence[: limit - 1] + "…"


# ---- checks driven by the tool results -----------------------------------------------------------------------


def _check_result(chk: _Checker, tool: str, result: dict[str, Any], spec: dict[str, Any]) -> None:
    answer = chk.answer
    health = _mapping(result.get("data_health"))
    if not health and "stale_after_days" in result and "latest_data" in result:  # get_data_status carries its own
        derived = {"IMPORT_FAILED": "IMPORT_INCOMPLETE", "STALE": "STALE", "NO_DATA": "INSUFFICIENT_DATA"}
        health = {"status": derived.get(str(result.get("status"))), "reasons": [result.get("reason")]}
    status = health.get("status")
    reasons = [str(r) for r in health.get("reasons") or [] if r]
    tier = _mapping(_mapping(result.get("claim")).get("decision")).get("tier")
    where = f"{tool} result"

    if status == "IMPORT_INCOMPLETE":
        chk.ran_layer(INTERPRETATION)
        if not _any(IMPORT_FAILED, answer):
            chk.fail(
                INTERPRETATION,
                "import_failure_omitted",
                f"{where} has data_health IMPORT_INCOMPLETE; the answer never says an import failed",
            )
    if status == "STALE":
        chk.ran_layer(INTERPRETATION)
        if not _any(STALE, answer):
            latest = _dig(health, "freshness.latest_data")
            chk.fail(
                INTERPRETATION,
                "stale_data_not_disclosed",
                f"{where} has data_health STALE (latest data {latest}); the answer never says the data is old",
            )
    if status == "MIXED_SOURCES":
        chk.ran_layer(INTERPRETATION)
        if not _any(CONFLICT, answer):
            chk.fail(
                INTERPRETATION,
                "source_conflict_omitted",
                f"{where} has data_health MIXED_SOURCES ({', '.join(reasons[:2])}); "
                "the answer never mentions that sources disagree",
            )
    if status == "INSUFFICIENT_DATA" or tier == "insufficient":
        chk.ran_layer(INTERPRETATION)
        if not _any(INSUFFICIENT, answer):
            chk.fail(
                INTERPRETATION,
                "insufficient_data_not_acknowledged",
                f"{where} is {status or 'tier insufficient'}; "
                "the answer states a conclusion without saying there is not enough data",
            )
    if tier == "suggestive":
        chk.ran_layer(INTERPRETATION)
        if not _any(HEDGE, answer):
            chk.fail(
                INTERPRETATION,
                "suggestive_presented_as_supported",
                f"{where} has claim tier 'suggestive'; the answer has no hedging (suggests/may/appears/...)",
            )
    if tier in ("suggestive", "insufficient"):
        sentence = _affirmative(CERTAINTY, answer)
        if sentence:
            chk.fail(
                INTERPRETATION,
                "overconfident_conclusion",
                f'{where} has claim tier {tier!r}, but the answer uses certainty language: "{_excerpt(sentence)}"',
            )
    if status in ("STALE", "INSUFFICIENT_DATA", "MIXED_SOURCES", "IMPORT_INCOMPLETE", "VALID_WITH_GAPS"):
        sentence = _affirmative(CLEAN_CLAIMS, answer, local=True)
        if sentence:
            chk.ran_layer(INTERPRETATION)
            chk.fail(
                INTERPRETATION,
                "contradicts_data_health",
                f'{where} has data_health {status}, but the answer says: "{_excerpt(sentence)}"',
            )

    association = "r" in result and "metric_a" in result
    if association or spec.get("forbid_causal"):
        chk.ran_layer(INTERPRETATION)
        sentence = _affirmative(CAUSAL, answer)
        if sentence:
            chk.fail(
                INTERPRETATION,
                "causal_claim_unsupported",
                f'{where} is an association; the answer states a cause: "{_excerpt(sentence)}"',
            )

    no_observations = "no_observations_in_window" in reasons or (
        isinstance(result.get("points"), list) and not result["points"]
    )
    if no_observations:
        chk.ran_layer(GROUNDING)
        sentence = _affirmative(ZERO_VALUE, answer, local=True)
        if sentence:
            chk.fail(
                GROUNDING,
                "missing_data_as_zero",
                f'{where} has no observations in the window; the answer reports a zero value: "{_excerpt(sentence)}"',
            )


# ---- checks driven by the scenario ---------------------------------------------------------------------------


def _check_spec(chk: _Checker, trace: Trace, spec: dict[str, Any], today: date) -> None:
    ok_results = {c.tool: c.result for c in trace.tool_calls if c.registered and not c.is_error}

    def lookup(entry: dict[str, Any]) -> Any:
        return _dig(ok_results.get(entry["tool"]), entry["path"]) if entry["tool"] in ok_results else None

    for entry in spec.get("values", []):
        chk.ran_layer(GROUNDING)
        expected = lookup(entry)
        label = f"{entry['tool']}.{entry['path']}"
        if isinstance(expected, bool) or not isinstance(expected, (int, float)):
            chk.fail(
                GROUNDING,
                "value_missing_from_results",
                f"{label} is not a number in the tool result (got {expected!r}), so it cannot be checked",
            )
            continue
        found, with_unit = _states_number(chk.answer, float(expected), entry.get("unit"))
        if not found:
            chk.fail(GROUNDING, entry.get("code", "value_not_stated"), f"the answer never states {label} = {expected}")
        elif not with_unit:
            chk.fail(
                GROUNDING,
                "wrong_unit",
                f"the answer states {label} = {expected} but not with the unit /{entry['unit']}/",
            )

    for entry in spec.get("dates", []):
        chk.ran_layer(GROUNDING)
        raw = lookup(entry) if isinstance(entry, dict) else entry
        try:
            day = date.fromisoformat(str(raw))
        except ValueError:
            chk.fail(
                GROUNDING, "date_missing_from_results", f"{entry!r} is not a date in the tool result (got {raw!r})"
            )
            continue
        if not _states_date(chk.answer, day):
            chk.fail(GROUNDING, "date_not_stated", f"the answer never states the date {day.isoformat()}")

    for key, forbidden in (("require", False), ("forbid", True)):
        for entry in spec.get(key, []):
            chk.ran_layer(entry["layer"])
            patterns = _rx(*entry["any"])
            if forbidden:
                sentence = _affirmative(patterns, chk.answer)
                if sentence:
                    chk.fail(entry["layer"], entry["code"], f'forbidden content: "{_excerpt(sentence)}"')
            elif not _any(patterns, chk.answer):
                chk.fail(
                    entry["layer"], entry["code"], f"required content missing (none of: {', '.join(entry['any'])})"
                )


def score_answer(spec: dict[str, Any], trace: Trace, today: date) -> AnswerScore:
    """Score the final answer of one trace. `spec` is the scenario's `answer` mapping; an empty spec scores nothing."""
    if not spec:
        return AnswerScore(None, None)
    chk = _Checker(trace.final_answer.strip())
    if not chk.answer:
        chk.ran_layer(GROUNDING)
        chk.fail(
            GROUNDING, "no_final_answer", f"the agent ended without a final answer (terminated: {trace.terminated})"
        )
        return AnswerScore(None, False, chk.failures)
    for call in trace.tool_calls:
        if call.registered and not call.is_error and isinstance(call.result, dict):
            _check_result(chk, call.tool, call.result, spec)
    _check_spec(chk, trace, spec, today)

    def verdict(layer: str) -> bool | None:
        if layer not in chk.ran:
            return None
        return not any(f["layer"] == layer for f in chk.failures)

    return AnswerScore(verdict(INTERPRETATION), verdict(GROUNDING), chk.failures)
