#!/usr/bin/env bash
# Publish the verified engineering-audit findings as GitHub issues.
#
#   bash tools/publish-audit-issues.sh                 dry run: reports what would be created, creates nothing
#   bash tools/publish-audit-issues.sh --show-body     dry run that also prints each issue body
#   bash tools/publish-audit-issues.sh --publish       actually create the missing issues
#
# Safe to re-run. Each issue carries a stable audit id (QSM-AUDIT-NNN) in its title and in a hidden marker in
# its body. Before creating anything the script reads every issue in the repository, open AND closed, and skips
# any audit id that already appears, whatever its state. It never creates labels: a label is attached only if
# it already exists in the repository. It never prints credentials (gh's own login check is silenced).
set -euo pipefail

REPO="Thecimal/quantified-self-mcp"
PUBLISH=0
SHOW_BODY=0

usage() {
  cat <<'USAGE'
Usage: bash tools/publish-audit-issues.sh [--publish] [--show-body]

  (no flags)   dry run: list what would be created or skipped; nothing is created
  --show-body  in a dry run, also print each issue body
  --publish    create the issues that do not exist yet (open or closed)
USAGE
}

for arg in "$@"; do
  case "$arg" in
    --publish) PUBLISH=1 ;;
    --show-body) SHOW_BODY=1 ;;
    -h | --help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $arg" >&2
      usage >&2
      exit 2
      ;;
  esac
done

die() {
  echo "ERROR: $*" >&2
  exit 1
}

# ---------------------------------------------------------------------------
# 1. gh authentication
# ---------------------------------------------------------------------------
command -v gh >/dev/null 2>&1 || die "GitHub CLI (gh) is not installed."
gh auth status >/dev/null 2>&1 || die "gh is not authenticated (run: gh auth login). Nothing was created."

# ---------------------------------------------------------------------------
# 2. Existing issues, open and closed (pull requests excluded)
#    One TSV line per issue: number, state, audit ids found in its title or body marker.
#    A failed lookup must stop the run: an empty result would otherwise look like "no duplicates".
# ---------------------------------------------------------------------------
EXISTING="$(gh api --paginate "repos/${REPO}/issues?state=all&per_page=100" --jq '
  .[] | select(.pull_request == null)
  | [ (.number | tostring), .state,
      ( [ (.title // ""), (.body // "") ]
        | map(scan("(?:audit-id: |^\\[)(QSM-AUDIT-[0-9]{3})") | .[0])
        | unique | join(",") ) ]
  | @tsv')" || die "Could not list the existing issues of ${REPO}. Nothing was created."

ISSUES_SCANNED="$(grep -c . <<<"$EXISTING" || true)"

# Prints "#N (state)" for the first issue that carries the audit id; returns 1 if none does.
existing_for() {
  local id="$1" number state ids
  while IFS=$'\t' read -r number state ids; do
    case ",${ids}," in
      *",${id},"*)
        echo "#${number} (${state})"
        return 0
        ;;
    esac
  done <<<"$EXISTING"
  return 1
}

# ---------------------------------------------------------------------------
# 3. Existing labels (never created here)
# ---------------------------------------------------------------------------
LABELS="$(gh api --paginate "repos/${REPO}/labels?per_page=100" --jq '.[].name')" ||
  die "Could not list the labels of ${REPO}. Nothing was created."

has_label() {
  grep -Fxq -- "$1" <<<"$LABELS"
}

# ---------------------------------------------------------------------------
# The selected issues, ranked by severity. Every claim in a body was checked against the repository at
# 4cdbc67 (reproduced on a temporary database with synthetic data, or read from the cited file and test).
# ---------------------------------------------------------------------------
AUDIT_IDS=(QSM-AUDIT-001 QSM-AUDIT-002 QSM-AUDIT-003 QSM-AUDIT-004 QSM-AUDIT-005)

issue_title() {
  case "$1" in
    QSM-AUDIT-001) echo "[QSM-AUDIT-001] P0: read_workout_sessions and log_workout_session return metrics listed in HEALTH_PRIVATE_FIELDS" ;;
    QSM-AUDIT-002) echo "[QSM-AUDIT-002] P0: log_measurement stores a reading in a unit that conflicts with the metric's canonical unit" ;;
    QSM-AUDIT-003) echo "[QSM-AUDIT-003] P1: Health Connect import adds steps and sleep up across data origins and keeps no origin" ;;
    QSM-AUDIT-004) echo "[QSM-AUDIT-004] P1: get_metric_provenance reports sources that agree on a sum metric's day total as conflicting" ;;
    QSM-AUDIT-005) echo "[QSM-AUDIT-005] P1: Apple Health import drops valid per-source sleep when two sources add up to more than 24 h" ;;
    *) die "no title defined for $1" ;;
  esac
}

# Candidate labels, attached only if they already exist in the repository.
issue_labels() {
  case "$1" in
    QSM-AUDIT-001) echo "bug privacy P0" ;;
    QSM-AUDIT-002) echo "bug P0" ;;
    QSM-AUDIT-003) echo "bug P1" ;;
    QSM-AUDIT-004) echo "bug P1" ;;
    QSM-AUDIT-005) echo "bug P1" ;;
    *) die "no labels defined for $1" ;;
  esac
}

issue_body() {
  case "$1" in
    QSM-AUDIT-001)
      cat <<'BODY'
<!-- audit-id: QSM-AUDIT-001 -->
**Audit id:** QSM-AUDIT-001 · **Severity:** P0 (privacy leak) · **Size:** one implementation run

## Problem
`log_workout_session` and `read_workout_sessions` return the values of metrics listed in `HEALTH_PRIVATE_FIELDS`. `privacy.py` documents that private metrics are "never exposed by any tool, no matter how they're stored", and the README says they are "excluded from MCP read and analytical operations". These two tools are the exception, and the privacy-boundary test records them as a known gap.

## Evidence (current `main`)
- Reproduced on a temporary database with `HEALTH_PRIVATE_FIELDS=workout_minutes,heart_rate`: `log_workout_session(date="2026-10-01", activity_type="run", duration_minutes=137, avg_heart_rate=151, max_heart_rate=183)`, then `read_workout_sessions()`. Both responses contain `"duration_minutes": 137, "avg_heart_rate": 151, "max_heart_rate": 183`.
- `tests/test_privacy_boundary.py::test_workout_sessions_respect_private_workout_minutes` is a `strict=True` xfail whose reason is this defect; it fails when run with `--runxfail`. It asserts only the duration; the heart-rate values above come from the reproduction.
- `PRIVACY_BOUNDARY` in the same file classifies both tools as `KNOWN_GAP`.
- `schemas.py::WorkoutSessionRow`: `duration_minutes` is a required `int`; `avg_heart_rate` and `max_heart_rate` are already `int | None`.
- A different workout path was already fixed in `1d843c6` ("hide private heart rate in explain_metric_change workout sessions"); the two direct tools were not.

## Impact
A user who marks workout minutes or heart rate private still has them returned to the model (and, with a cloud model, sent to the provider) through these two tools, which defeats the control the README advertises.

## Proposed minimal solution
Follow the contract `privacy.py` already documents (a private metric is reported as `null`, the stored value is kept): in `tools/workouts.py`, null `duration_minutes` when `workout_minutes` is in `PRIVATE_FIELDS`, and `avg_heart_rate` / `max_heart_rate` when `heart_rate` is in `PRIVATE_FIELDS`, in the session returned by both tools (the read and the write echo). Widen `WorkoutSessionRow.duration_minutes` to `int | None` so the output schema allows the null; this is the only public-schema change. Do not refuse the tools, do not change what is stored, do not change behaviour when the fields are not private. If you prefer refusing `read_workout_sessions` outright (the `REFUSES` convention), say so in the pull request; redaction is the default because it is what `privacy.py` documents.

## Definition of Done
- [ ] With `HEALTH_PRIVATE_FIELDS=workout_minutes,heart_rate`, the reproduction above returns `null` for `duration_minutes`, `avg_heart_rate` and `max_heart_rate` from both tools, and `SELECT duration_minutes, avg_heart_rate, max_heart_rate FROM workout_sessions` on that database still returns `137, 151, 183`.
- [ ] With only `workout_minutes` private the heart rates are still returned; with only `heart_rate` private `duration_minutes` is still returned.
- [ ] With `HEALTH_PRIVATE_FIELDS` unset both tools' output is unchanged and every existing workout test passes unmodified.
- [ ] The `xfail` marker is removed from `test_workout_sessions_respect_private_workout_minutes` and it passes; both tools move from `KNOWN_GAP` to `REDACTS` in `PRIVACY_BOUNDARY` (drop the `KNOWN_GAP` constant if nothing else uses it); a new test covers the two heart-rate fields.
- [ ] `pytest tests/test_privacy_boundary.py -q -rx` lists no xfail for the workout test; `ruff check .`, `mypy .` and `pytest -q` are clean; `CHANGELOG.md` has an entry.

## Verification command
```bash
pytest tests/test_privacy_boundary.py tests/test_explain_metric_change_privacy.py -q -rx && ruff check . && mypy . && pytest -q
```

## Relevant files
`tools/workouts.py`, `schemas.py` (`WorkoutSessionRow`), `privacy.py` (read only), `tests/test_privacy_boundary.py`, `CHANGELOG.md`.
BODY
      ;;
    QSM-AUDIT-002)
      cat <<'BODY'
<!-- audit-id: QSM-AUDIT-002 -->
**Audit id:** QSM-AUDIT-002 · **Severity:** P0 (incorrect health results) · **Size:** one implementation run

## Problem
The `log_measurement` MCP tool accepts a `unit` that conflicts with the metric's canonical unit and stores the value unconverted. Nothing rejects it and nothing converts it, so the number lands in `daily_metrics` as if it were already in the canonical unit.

## Evidence (reproduced on `main` with synthetic data in a temporary database, through the real MCP client)
| call | stored in `measurements` | result |
|---|---|---|
| `metric="weight_kg", value=154.3, unit="lb"` | `(weight_kg, 154.3, 'lb')` | accepted; `daily_metrics.weight_kg = 154.3` |
| `metric="water_ml", value=1.5, unit="L"` | `(water_ml, 1.5, 'L')` | accepted; `daily_metrics.water_ml = 1.5` (1.5 mL, not 1500) |

- `tools/measurements.py::log_measurement` only runs `validate_metrics({metric: value})` (bounds: `weight_kg` 1-500, `water_ml` 0-10000, which both values pass) and passes `unit` straight to `logic.insert_measurement`.
- `metric_registry.py` declares the canonical unit per metric (`weight_kg` = `kg`, `water_ml` = `mL`, `steps` = `count`, `sleep_hours` = `h`, ...); `mood` has none. The write path never consults it.
- The argument docstring says `unit: Unit the value is in, e.g. "bpm", "kg". Optional.`, which invites a client to pass whatever unit the user spoke.
- The Apple Health importer converts lb to kg and L to mL, pinned by `tests/test_pipeline_end_to_end.py`. A search of `tests/` for a `log_measurement` unit-mismatch case found none. Not audited here: the unit handling in `ingest.py`.

## Impact
A user (or the model on their behalf) who says "I weigh 154 lb" gets 154 stored as kg. For `weight_kg` (aggregation `last`) it becomes the day's value and feeds baselines, trends and explanations. Water logged in litres is stored as millilitres.

## Proposed minimal solution
In `tools/measurements.py::log_measurement`, after the metric is validated: if `unit` is given and non-blank and the metric's registry unit is not `None`, compare them case-insensitively. On a mismatch raise the existing `ERR_INVALID_METRIC_VALUE` tool error that names the expected unit and tells the caller to convert first. Reject; do not convert. Update the `unit` argument docstring to say it must match the metric's unit or be omitted. No schema change, no importer change, no new tool, no unit-alias table.

## Definition of Done
- [ ] `log_measurement(metric="weight_kg", value=154.3, unit="lb")` raises a tool error with code `ERR_INVALID_METRIC_VALUE` whose message contains `kg`; `SELECT COUNT(*) FROM measurements` is unchanged.
- [ ] The same for `metric="water_ml", unit="L"` (message contains `mL`).
- [ ] `unit` omitted, `unit="kg"` and `unit="KG"` for `weight_kg` are stored exactly as before.
- [ ] A metric whose registry unit is `None` (`mood`) still logs unchanged, with or without a `unit`.
- [ ] New tests in `tests/test_log_measurement_units.py` fail on current `main` and pass after the change; `tests/test_measurement_tools.py` passes unchanged.
- [ ] `ruff check .`, `mypy .` and `pytest -q` are clean; no file under `db/` and not `import_adapters.py` is modified; `CHANGELOG.md` has an entry.

## Verification command
```bash
pytest tests/test_log_measurement_units.py tests/test_measurement_tools.py -q && ruff check . && mypy . && pytest -q
```

## Relevant files
`tools/measurements.py`, `metric_registry.py`, `logic.py` (`insert_measurement`, read only), `tests/test_measurement_tools.py`, new `tests/test_log_measurement_units.py`, `CHANGELOG.md`.
BODY
      ;;
    QSM-AUDIT-003)
      cat <<'BODY'
<!-- audit-id: QSM-AUDIT-003 -->
**Audit id:** QSM-AUDIT-003 · **Severity:** P1 (provenance, double counting) · **Size:** one implementation run

## Problem
`adapt_health_connect` sums `steps` and `sleep_hours` per day across every app (data origin) that wrote them and stores the total under the importer's name. Two apps recording the same walk or the same night, for example a phone and a watch, are added together and no origin survives. The Apple Health importer had the same defect for sleep and was fixed in `4cdbc67`; Health Connect was not touched.

## Evidence (reproduced on `main` with a synthetic export in a temporary database)
- Two `StepsRecord`s on one day, 4000 each, different ids and `metadata.dataOrigin.packageName` (`com.a.phone`, `com.b.watch`): one row `(steps, 8000.0, source='health-connect')`; `daily_metrics.steps = 8000`, `source_count = 1`.
- Two overlapping 6 h `SleepSessionRecord`s from the same two origins: one row `(sleep_hours, 12.0, source='health-connect')`.
- `adapt_health_connect` returns day rows only (no `raw_measurements`) and reads only `metadata.id`, for repeat detection. Nothing outside `tests/` reads `dataOrigin`.
- The serialized shape is already established by the fixtures in `tests/test_health_connect_duplicates.py` (`metadata["dataOrigin"] = {"packageName": origin}`), whose comment says which origin should win "is a source-resolution question, not this one's".
- The adapter's docstring lists `steps`, `workout_minutes` and `sleep_hours` as summed per day. `7dc4d4b` (repeated Health Connect records) removes exact repeats only; different origins are kept and then summed.
- For comparison, the Apple Health importer now stores one day total per `sourceName` and the daily view resolves between sources (`source_count = 2`, no summing); see `tests/test_apple_health_sleep_provenance.py`.

## Impact
Anyone whose Health Connect holds the same steps or sleep from two apps gets inflated daily steps and sleep, and source coverage reports a single source (`health-connect`) although the data came from several.

## Proposed minimal solution
Mirror `adapt_apple_health` for `steps` and `sleep_hours` only: emit one raw observation per (day, origin) with `source` = `metadata.dataOrigin.packageName` when present, at the project's noon day-level timestamp (`T12:00:00`), and leave the adapter's day `rows` as they are for validation and reporting. A record without a `dataOrigin` keeps `source=None`, so import stamps the importer's name; no origin is invented. No source-precedence rule, no schema change, no change to other record types. `workout_minutes` follows the same pattern and is left to a follow-up to keep this task small.

## Definition of Done
- [ ] Two-origin steps fixture (4000 + 4000, one day): `measurements` holds two `steps` rows with distinct `source`; `daily_metrics.steps = 4000` and `source_count = 2` (not 8000).
- [ ] Two overlapping 6 h sleep sessions from distinct origins: `daily_metrics.sleep_hours = 6.0` (not 12.0).
- [ ] A record without `dataOrigin` is stored with `source='health-connect'` (the importer's name); no origin is invented.
- [ ] Re-importing the same export prints `0 added, 0 updated`; `invariant.verify` reports `ok`.
- [ ] `tests/test_import_adapters.py` and `tests/test_health_connect_duplicates.py` pass; change only an assertion that pins the old stored source name, and say so in the commit message.
- [ ] A new `tests/test_health_connect_provenance.py` fails on current `main` and passes after the change; `CHANGELOG.md` has an entry; `ruff check .`, `mypy .` and `pytest -q` are clean.

## Verification command
```bash
pytest tests/test_health_connect_provenance.py tests/test_health_connect_duplicates.py tests/test_import_adapters.py -q && ruff check . && mypy . && pytest -q
```

## Relevant files
`import_adapters.py` (`adapt_health_connect`; `adapt_apple_health` as the pattern), `init_db.py` (raw-observation path, read only), `tests/test_import_adapters.py`, `tests/test_health_connect_duplicates.py`, `docs/clients/android-health-connect.md`, `CHANGELOG.md`.
BODY
      ;;
    QSM-AUDIT-004)
      cat <<'BODY'
<!-- audit-id: QSM-AUDIT-004 -->
**Audit id:** QSM-AUDIT-004 · **Severity:** P1 (evidence integrity) · **Size:** one implementation run

## Problem
`get_metric_provenance` sets `conflict` by comparing each source's average reading for the day. For a metric aggregated by `sum` (steps, sleep, water, workout minutes) the average is not the source's day total, so two sources that agree on the total are reported as conflicting whenever they log in different numbers of records. Other tools tell the model to call this one "to see whether they actually disagree".

## Evidence (current `main`)
- `logic.py::get_metric_provenance` builds each source's `value` as the mean of its readings and computes `conflict` from those means (`CONFLICT_TOLERANCE_PCT = 0.05`). The docstring documents the average.
- `metric_registry.py` aggregates `steps`, `sleep_hours`, `workout_minutes` and `water_ml` by `sum`.
- `tests/test_pipeline_end_to_end.py::test_sources_that_agree_on_the_day_total_are_not_reported_as_a_conflicting_reading` is a `strict=True` xfail recording this defect: Watch logs 500 + 500, Phone logs 1000 steps (both say 1000 for the day), and `provenance["conflict"]` is `True`. Run with `--runxfail` it fails with `assert True is False`.
- Genuine disagreement is pinned elsewhere and must keep working: `tests/test_logic.py` (resting heart rate conflict, close readings, single source) and `tests/test_agent_eval.py::test_conflicting_fixture_has_two_disagreeing_sources` (Apple Watch vs Garmin steps, one reading each per day, 900 apart).

## Impact
For the common case of a watch and a phone counting the same steps, the model is told the sources disagree, and may then discount or caveat correct data, or ask the user to reconcile sources that already agree.

## Proposed minimal solution
In `logic.py::get_metric_provenance`, for a metric whose registry aggregation is `sum`, compute `conflict` from each source's day total (the sum of its readings) instead of its average; keep the average-based comparison for `mean` and `last` metrics and keep the existing tolerance and the `lo == 0` rule. Leave `sources[*].value`, `n`, `latest_timestamp`, their order and the output schema unchanged (the documented average stays); update the docstring and the `get_metric_provenance` entry in `docs/tools.md` to say how `conflict` is computed. Changing `value` itself would alter a documented field and is out of scope.

## Definition of Done
- [ ] The `xfail` marker is removed from `test_sources_that_agree_on_the_day_total_are_not_reported_as_a_conflicting_reading` and the test passes (`conflict` is `False`, sources are `{Watch, Phone}`).
- [ ] A new test: Watch totals 1000 and Phone totals 2000 for one day of steps, with different record counts, gives `conflict is True`.
- [ ] A new test: a `mean` metric is unchanged (`resting_heart_rate` 58 vs 70 is a conflict; 60 vs 61 is not).
- [ ] `tests/test_logic.py`, `tests/test_agent_eval.py`, `tests/test_source_resolution.py` and `tests/test_measurement_tools.py` pass without assertion edits.
- [ ] `sources[*].value`, `n` and `latest_timestamp` are identical to current `main` for the fixtures above.
- [ ] `ruff check .`, `mypy .` and `pytest -q` are clean (the full suite reports one xfail fewer); `CHANGELOG.md` has an entry.

## Verification command
```bash
pytest tests/test_pipeline_end_to_end.py tests/test_logic.py tests/test_agent_eval.py -q -rx && ruff check . && mypy . && pytest -q
```

## Relevant files
`logic.py` (`get_metric_provenance`), `metric_registry.py` (read only), `tests/test_pipeline_end_to_end.py`, `tests/test_logic.py`, `docs/tools.md`, `CHANGELOG.md`.
BODY
      ;;
    QSM-AUDIT-005)
      cat <<'BODY'
<!-- audit-id: QSM-AUDIT-005 -->
**Audit id:** QSM-AUDIT-005 · **Severity:** P1 (import reliability) · **Size:** one implementation run

## Problem
The Apple Health importer validates sleep against the 0-24 h bound on the sum across sources, although it stores one total per source (`4cdbc67`). When several sources each record a long night the sum exceeds 24 h and the whole day is dropped, including every valid per-source total.

## Evidence (reproduced on `main` with a synthetic export in a temporary database)
- "Synthetic Watch" 13 h and "Synthetic App" 13 h on the same night, plus a 7 h night from the Watch the next day: stderr reports `Skipping ... date 2026-03-01: sleep_hours must be between 0 and 24, got 26.0`; `measurements` holds only the next day's `(2026-03-02T12:00:00, sleep_hours, 7.0, 'Synthetic Watch')`; `imports.rows_skipped = 1`.
- `adapt_apple_health` still builds each day row's `sleep_hours` from the cross-source sum. `init_db.py` runs `validate_metrics` on `adapted.rows`, records `skipped_days`, then drops every raw observation of those days, so the per-source rows are discarded with it.
- Since `4cdbc67` the stored sleep comes from the per-source raw observations; the day row's value is not stored (`measurement_rows_from_daily` skips a metric that raw observations cover).
- `tests/test_apple_health_sleep_provenance.py` has no case near the bound.

## Impact
Valid nights from several sources are lost together, the sleep series gets a gap, freshness and coverage look worse, and `rows_skipped` counts a skip that is not a data error. Three apps recording 9 h each is enough to trigger it.

## Proposed minimal solution
In `adapt_apple_health` only: bound-check each (source, day) sleep total with the existing `_check_observation_bounds`, skipping just the offending source-day (add it to the returned `skipped` count and print a stderr line naming the source and the day), and stop putting the cross-source sum into the day row's `sleep_hours` so `init_db.py` no longer rejects the day on it. Before changing the row, read every consumer of `AdaptedImport.rows` and `present_columns` in `init_db.py` and the tests; if one needs the day's sleep value, stop and describe it in the issue instead of choosing a value. Do not pick a winning source, do not use `max` or any other precedence rule, do not touch other adapters, no schema change.

## Definition of Done
- [ ] The fixture above stores `(2026-03-01T12:00:00, 13.0, 'Synthetic Watch')`, `(2026-03-01T12:00:00, 13.0, 'Synthetic App')` and `(2026-03-02T12:00:00, 7.0, 'Synthetic Watch')`; `daily_metrics.sleep_hours` for 2026-03-01 is `13.0` with `source_count = 2`; no `Skipping` line on stderr; `imports.rows_skipped = 0`.
- [ ] One source whose intervals total 25 h on one day is skipped (`rows_skipped = 1`, stderr names the source and the day) while another source's valid 6 h for the same day is stored.
- [ ] `tests/test_apple_health_sleep_provenance.py`, `tests/test_import_adapters.py`, `tests/test_pipeline_end_to_end.py` and `tests/test_source_stamping.py` pass without assertion edits.
- [ ] New tests fail on current `main` and pass after the change; `CHANGELOG.md` has an entry; `ruff check .`, `mypy .` and `pytest -q` are clean.

## Verification command
```bash
pytest tests/test_apple_health_sleep_provenance.py tests/test_import_adapters.py tests/test_pipeline_end_to_end.py tests/test_source_stamping.py -q && ruff check . && mypy . && pytest -q
```

## Relevant files
`import_adapters.py` (`adapt_apple_health`, `_check_observation_bounds`), `init_db.py` (day-row validation and `skipped_days`, read only), `tests/test_apple_health_sleep_provenance.py`, `CHANGELOG.md`.
BODY
      ;;
    *) die "no body defined for $1" ;;
  esac
}

# ---------------------------------------------------------------------------
# 4. Create (or, in a dry run, report)
# ---------------------------------------------------------------------------
if ((PUBLISH)); then MODE="PUBLISH"; else MODE="dry run (nothing will be created)"; fi
echo "Repository: ${REPO}"
echo "Mode:       ${MODE}"
echo "Issues scanned (open and closed): ${ISSUES_SCANNED}"
echo

CREATED=0
SKIPPED=0
PLANNED=0
FAILED=0

for id in "${AUDIT_IDS[@]}"; do
  title="$(issue_title "$id")"

  if found="$(existing_for "$id")"; then
    echo "SKIP    ${id}  already exists as ${found}"
    SKIPPED=$((SKIPPED + 1))
    continue
  fi

  label_args=()
  applied=()
  missing=()
  for label in $(issue_labels "$id"); do
    if has_label "$label"; then
      label_args+=(--label "$label")
      applied+=("$label")
    else
      missing+=("$label")
    fi
  done
  label_note="labels: ${applied[*]-none}"
  if ((${#missing[@]} > 0)); then label_note="${label_note}; not in repository, not applied: ${missing[*]}"; fi

  if ((PUBLISH)); then
    if out="$(issue_body "$id" | gh issue create --repo "$REPO" --title "$title" --body-file - ${label_args[@]+"${label_args[@]}"} 2>&1)"; then
      url="$(tail -n 1 <<<"$out")"
      if [[ "$url" =~ ^https://github\.com/[^/]+/[^/]+/issues/[0-9]+$ ]]; then
        echo "CREATED ${id}  ${url}  (${label_note})"
        CREATED=$((CREATED + 1))
      else
        echo "FAILED  ${id}  gh reported success but returned no issue URL: ${out}" >&2
        FAILED=$((FAILED + 1))
      fi
    else
      echo "FAILED  ${id}  ${out}" >&2
      FAILED=$((FAILED + 1))
    fi
  else
    echo "WOULD CREATE ${id}  (${label_note})"
    echo "             ${title}"
    PLANNED=$((PLANNED + 1))
    if ((SHOW_BODY)); then
      echo "-------------------------------------------------------------------------"
      issue_body "$id"
      echo "-------------------------------------------------------------------------"
    fi
  fi
done

echo
if ((PUBLISH)); then
  echo "Summary: created ${CREATED}, skipped (already exist) ${SKIPPED}, failed ${FAILED}."
else
  echo "Summary (dry run): would create ${PLANNED}, skipped (already exist) ${SKIPPED}. Nothing was created."
  if ((PLANNED > 0)); then echo "To create them: bash tools/publish-audit-issues.sh --publish"; fi
fi

if ((FAILED > 0)); then
  echo "Some issues were not created. Re-run after fixing the error; issues that were created are skipped." >&2
  exit 1
fi
