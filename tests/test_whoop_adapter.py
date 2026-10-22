"""Tests for import_adapters.adapt_whoop: JSON saved from the WHOOP Developer API (v2).

Every fixture here is synthetic, built from the field names in WHOOP's v2 API sample responses; no real
account data is involved, and nothing here proves compatibility with any other API version.
"""

import copy
import json
import random
import sqlite3
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from import_adapters import ADAPTERS, adapt_whoop, detect_adapter  # noqa: E402
from init_db import init_health_db  # noqa: E402
from source_coverage import KNOWN_STATISTICS  # noqa: E402

SAMPLE = REPO / "sample_data" / "whoop_sample.json"
SLEEP_A = "11111111-1111-4111-8111-111111111111"


def _sample_records() -> list[dict]:
    return json.loads(SAMPLE.read_text(encoding="utf-8"))["records"]


def _write(tmp_path, payload, name="whoop.json") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _sleep(i, start, end, offset="-05:00", light=14_400_000, sws=6_300_000, rem=6_600_000, nap=False, **extra):
    record = {
        "id": i,
        "start": start,
        "end": end,
        "timezone_offset": offset,
        "nap": nap,
        "score_state": "SCORED",
        "score": {
            "stage_summary": {
                "total_in_bed_time_milli": 30_000_000,
                "total_awake_time_milli": 2_400_000,
                "total_light_sleep_time_milli": light,
                "total_slow_wave_sleep_time_milli": sws,
                "total_rem_sleep_time_milli": rem,
            }
        },
    }
    record.update(extra)
    return record


def _recovery(cycle, sleep_id, rhr=52, hrv=61.4, state="SCORED"):
    record = {"cycle_id": cycle, "sleep_id": sleep_id, "score_state": state}
    if state == "SCORED":
        record["score"] = {"recovery_score": 70, "resting_heart_rate": rhr, "hrv_rmssd_milli": hrv}
    return record


def _workout(i, start, end, offset="-05:00", **extra):
    return {"id": i, "start": start, "end": end, "timezone_offset": offset, "sport_id": 1, **extra}


# ---- valid import, units, provenance ------------------------------------------------------------------
def test_sample_file_imports_to_the_expected_days_and_observations():
    result = adapt_whoop(SAMPLE)
    assert result.rows == [
        {"date": "2026-03-02", "sleep_hours": 7.58, "resting_heart_rate": 52, "hrv_ms": 61.4, "workout_minutes": 45},
        {"date": "2026-03-03", "sleep_hours": 7.0, "resting_heart_rate": 54, "hrv_ms": 58.0},
        {"date": "2026-03-05", "sleep_hours": 6.67, "resting_heart_rate": 50, "hrv_ms": 66.2},
    ]
    assert result.present_columns == ["sleep_hours", "resting_heart_rate", "hrv_ms", "workout_minutes"]
    assert result.skipped == 0
    # A pending-score recovery and a record of an unmapped kind are reported, not errors.
    assert result.unsupported_types == {"recovery (not scored)": 1, "unrecognised WHOOP record": 1}


def test_every_observation_keeps_its_source_and_canonical_unit():
    result = adapt_whoop(SAMPLE)
    assert len(result.raw_measurements) == 11
    assert {o["source"] for o in result.raw_measurements} == {"WHOOP"}
    units = {o["metric"]: o["unit"] for o in result.raw_measurements}
    assert units == {"sleep_hours": "h", "resting_heart_rate": "bpm", "hrv_ms": "ms", "workout_minutes": "min"}
    by_key = {(o["timestamp"], o["metric"]): o["value"] for o in result.raw_measurements}
    assert by_key[("2026-03-02T07:25:00", "sleep_hours")] == pytest.approx(27_300_000 / 3_600_000)  # asleep only
    assert by_key[("2026-03-02T18:30:00", "workout_minutes")] == 45.0
    assert by_key[("2026-03-02T07:25:00", "hrv_ms")] == 61.4  # already milliseconds


def test_only_time_actually_asleep_counts_not_awake_or_in_bed(tmp_path):
    path = _write(
        tmp_path,
        [_sleep(SLEEP_A, "2026-03-02T04:00:00.000Z", "2026-03-02T12:00:00.000Z", light=3_600_000, sws=0, rem=0)],
    )
    assert adapt_whoop(path).rows == [{"date": "2026-03-02", "sleep_hours": 1.0}]


def test_whoop_is_registered_and_its_hrv_is_known_to_be_rmssd():
    assert ADAPTERS["whoop"] is adapt_whoop
    assert KNOWN_STATISTICS[("whoop", "hrv_ms")] == "rmssd"
    assert detect_adapter(SAMPLE) == "csv"  # JSON is never auto-detected: --source whoop is required


def test_bare_list_and_records_wrapper_give_the_same_result(tmp_path):
    records = _sample_records()
    assert adapt_whoop(_write(tmp_path, records, "list.json")) == adapt_whoop(SAMPLE)
    assert adapt_whoop(_write(tmp_path, {"records": records}, "wrapped.json")) == adapt_whoop(SAMPLE)


# ---- timezone handling --------------------------------------------------------------------------------
def test_local_day_comes_from_the_records_own_offset_not_from_utc(tmp_path):
    records = [
        # 03:30 UTC at -05:00 is 22:30 the evening before: wakes on the 14th, local.
        _sleep("a", "2026-01-14T20:00:00.000Z", "2026-01-15T03:30:00.000Z", offset="-05:00"),
        # 23:30 UTC at +02:00 is 01:30 the next day.
        _sleep("b", "2026-01-15T20:00:00.000Z", "2026-01-15T23:30:00.000Z", offset="+02:00"),
        # 30-minute offset.
        _workout("w", "2026-01-16T10:00:00.000Z", "2026-01-16T10:30:00.000Z", offset="+05:30"),
    ]
    result = adapt_whoop(_write(tmp_path, records))
    stamps = {(o["metric"], o["timestamp"]) for o in result.raw_measurements}
    assert ("sleep_hours", "2026-01-14T22:30:00") in stamps
    assert ("sleep_hours", "2026-01-16T01:30:00") in stamps
    assert ("workout_minutes", "2026-01-16T15:30:00") in stamps
    assert all("+" not in o["timestamp"] and not o["timestamp"].endswith("Z") for o in result.raw_measurements)


def test_recovery_takes_the_local_time_of_its_sleep(tmp_path):
    records = [
        _recovery(1, "a", rhr=50, hrv=70.0),  # listed before its sleep on purpose
        _sleep("a", "2026-01-14T20:00:00.000Z", "2026-01-15T03:30:00.000Z", offset="-05:00"),
    ]
    result = adapt_whoop(_write(tmp_path, records))
    assert {o["timestamp"] for o in result.raw_measurements if o["metric"] == "hrv_ms"} == {"2026-01-14T22:30:00"}


# ---- malformed and missing fields ---------------------------------------------------------------------
@pytest.mark.parametrize(
    "mutate",
    [
        lambda r: r.update(end="not a time"),
        lambda r: r.update(end="2026-03-02T12:25:00.000"),  # no UTC marker
        lambda r: r.pop("timezone_offset"),
        lambda r: r.update(timezone_offset="EST"),
        lambda r: r.update(timezone_offset="-25:00"),
        lambda r: r["score"]["stage_summary"].pop("total_rem_sleep_time_milli"),
        lambda r: r["score"]["stage_summary"].update(total_rem_sleep_time_milli="lots"),
        lambda r: r["score"]["stage_summary"].update(total_rem_sleep_time_milli=float("nan")),
        lambda r: r["score"]["stage_summary"].update(total_light_sleep_time_milli=-1),
        lambda r: r["score"]["stage_summary"].update(total_light_sleep_time_milli=90_000_000),  # over 24 h
        lambda r: r.pop("id"),
        lambda r: r.update(id=["unhashable"]),
        lambda r: r["score"].pop("stage_summary"),
    ],
)
def test_a_bad_sleep_record_is_skipped_and_reported_and_the_rest_still_import(tmp_path, capsys, mutate):
    bad = _sleep(SLEEP_A, "2026-03-02T04:00:00.000Z", "2026-03-02T12:00:00.000Z")
    mutate(bad)
    good = _sleep("good", "2026-03-03T04:00:00.000Z", "2026-03-03T12:00:00.000Z")
    result = adapt_whoop(_write(tmp_path, [bad, good]))
    assert result.skipped == 1
    assert [row["date"] for row in result.rows] == ["2026-03-03"]
    assert "Skipping a record" in capsys.readouterr().err


def test_non_object_records_are_skipped(tmp_path):
    good = _sleep("good", "2026-03-03T04:00:00.000Z", "2026-03-03T12:00:00.000Z")
    result = adapt_whoop(_write(tmp_path, [7, "x", None, good]))
    assert result.skipped == 3 and len(result.rows) == 1


def test_recovery_without_its_sleep_in_the_file_is_skipped_not_guessed(tmp_path, capsys):
    result = adapt_whoop(_write(tmp_path, [_recovery(1, "no-such-sleep")]))
    assert result.skipped == 1 and result.rows == [] and result.raw_measurements == []
    assert "not a scored sleep in this file" in capsys.readouterr().err


@pytest.mark.parametrize(
    "mutate",
    [
        lambda r: r["score"].pop("resting_heart_rate"),
        lambda r: r["score"].pop("hrv_rmssd_milli"),
        lambda r: r["score"].update(hrv_rmssd_milli="n/a"),
        lambda r: r["score"].update(resting_heart_rate=400),  # outside bounds
        lambda r: r["score"].update(hrv_rmssd_milli=float("inf")),
    ],
)
def test_a_bad_recovery_keeps_neither_of_its_two_values(tmp_path, mutate):
    sleep = _sleep(SLEEP_A, "2026-03-02T04:00:00.000Z", "2026-03-02T12:00:00.000Z")
    bad = _recovery(1, SLEEP_A)
    mutate(bad)
    result = adapt_whoop(_write(tmp_path, [sleep, bad]))
    assert result.skipped == 1
    assert {o["metric"] for o in result.raw_measurements} == {"sleep_hours"}


def test_workout_that_ends_before_it_starts_is_skipped(tmp_path):
    result = adapt_whoop(_write(tmp_path, [_workout("w", "2026-03-02T12:00:00.000Z", "2026-03-02T11:00:00.000Z")]))
    assert result.skipped == 1 and result.rows == []


def test_unscored_records_are_unsupported_not_errors(tmp_path):
    unscored_sleep = _sleep("s", "2026-03-02T04:00:00.000Z", "2026-03-02T12:00:00.000Z", score_state="PENDING_SCORE")
    unscored_sleep.pop("score")
    pending_workout = _workout("w", "2026-03-02T17:00:00.000Z", "2026-03-02T17:30:00.000Z", score_state="PENDING_SCORE")
    result = adapt_whoop(_write(tmp_path, [unscored_sleep, pending_workout, _recovery(1, "s", state="UNSCORABLE")]))
    assert result.skipped == 0
    assert result.unsupported_types == {"sleep (not scored)": 1, "recovery (not scored)": 1}
    assert result.rows == [{"date": "2026-03-02", "workout_minutes": 30}]  # a workout's duration needs no score


@pytest.mark.parametrize("payload", ["{not json", '{"a": 1}', '"text"', "42"])
def test_unreadable_or_wrongly_shaped_file_exits_with_a_message(tmp_path, payload):
    path = tmp_path / "bad.json"
    path.write_text(payload, encoding="utf-8")
    with pytest.raises(SystemExit) as caught:
        adapt_whoop(path)
    assert "WHOOP API JSON" in str(caught.value)


def test_empty_export_is_a_valid_empty_import(tmp_path):
    result = adapt_whoop(_write(tmp_path, []))
    assert result.rows == [] and result.raw_measurements == [] and result.skipped == 0


# ---- duplicates, overlap, determinism -----------------------------------------------------------------
def test_records_repeated_in_the_file_count_once(tmp_path):
    records = _sample_records()
    doubled = adapt_whoop(_write(tmp_path, records + copy.deepcopy(records)))
    once = adapt_whoop(SAMPLE)
    assert (doubled.rows, doubled.raw_measurements, doubled.skipped) == (once.rows, once.raw_measurements, once.skipped)
    # an unscored record with an id is recognised as a repeat too (a record of unknown shape cannot be)
    assert doubled.unsupported_types["recovery (not scored)"] == once.unsupported_types["recovery (not scored)"] == 1


def test_a_nap_and_a_night_on_the_same_day_are_both_counted(tmp_path):
    records = [
        _sleep("night", "2026-03-03T03:00:00.000Z", "2026-03-03T11:00:00.000Z", light=7_200_000, sws=0, rem=0),
        _sleep("nap", "2026-03-03T17:00:00.000Z", "2026-03-03T18:00:00.000Z", light=3_600_000, sws=0, rem=0, nap=True),
    ]
    assert adapt_whoop(_write(tmp_path, records)).rows == [{"date": "2026-03-03", "sleep_hours": 3.0}]


def test_output_does_not_depend_on_record_order(tmp_path):
    records = _sample_records()
    expected = adapt_whoop(SAMPLE)
    for seed in range(5):
        shuffled = records[:]
        random.Random(seed).shuffle(shuffled)
        assert adapt_whoop(_write(tmp_path, shuffled, f"s{seed}.json")) == expected


# ---- through the real import pipeline (isolated database) --------------------------------------------
def _db(path: Path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def _import(source: Path, db: Path, **kwargs):
    init_health_db(source, db, replace=False, source="whoop", **kwargs)


def test_import_stores_provenance_and_projects_daily_values(tmp_path):
    db = tmp_path / "health.db"
    _import(SAMPLE, db)
    conn = _db(db)
    try:
        rows = conn.execute("SELECT * FROM measurements").fetchall()
        assert len(rows) == 11
        assert {r["source"] for r in rows} == {"WHOOP"}
        assert {r["importer"] for r in rows} == {"whoop"}
        assert {r["source_type"] for r in rows} == {"wearable"}
        daily = {(r["date"], r["metric"]): r for r in conn.execute("SELECT * FROM daily_metrics")}
        assert {r["resolved_source"] for r in daily.values()} == {"WHOOP"}
        assert daily[("2026-03-02", "workout_minutes")]["value"] == 45
        assert daily[("2026-03-02", "resting_heart_rate")]["value"] == 52
        night_and_nap = daily[("2026-03-03", "sleep_hours")]
        assert night_and_nap["value"] == pytest.approx(7.0, abs=0.01) and night_and_nap["raw_measurement_count"] == 2
        assert daily[("2026-03-05", "hrv_ms")]["value"] == pytest.approx(66.2)
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        conn.close()


def test_importing_the_same_file_twice_adds_nothing_and_changes_nothing(tmp_path):
    db = tmp_path / "health.db"
    _import(SAMPLE, db)
    conn = _db(db)
    first = [
        tuple(r) for r in conn.execute("SELECT id, timestamp, metric, value, imported_at FROM measurements ORDER BY id")
    ]
    conn.close()
    _import(SAMPLE, db)
    conn = _db(db)
    second = [
        tuple(r) for r in conn.execute("SELECT id, timestamp, metric, value, imported_at FROM measurements ORDER BY id")
    ]
    last = conn.execute(
        "SELECT records_added, records_updated, records_unchanged FROM imports ORDER BY id DESC LIMIT 1"
    ).fetchone()
    conn.close()
    assert second == first
    assert tuple(last) == (0, 0, 11)


def test_an_overlapping_reexport_with_a_correction_updates_in_place_without_duplicates(tmp_path):
    db = tmp_path / "health.db"
    _import(SAMPLE, db)
    corrected = _sample_records()
    for record in corrected:
        if record.get("sleep_id") == SLEEP_A:
            record["score"]["resting_heart_rate"] = 49
    corrected.append(_sleep("new", "2026-03-06T04:00:00.000Z", "2026-03-06T12:00:00.000Z"))
    _import(_write(tmp_path, corrected, "second.json"), db)
    conn = _db(db)
    try:
        per_key = conn.execute(
            "SELECT metric, substr(timestamp, 1, 10) AS day, COUNT(*) AS n FROM measurements GROUP BY 1, 2"
        ).fetchall()
        assert all(r["n"] <= 2 for r in per_key)  # at most the night and the nap on one day
        rhr = "SELECT value FROM measurements WHERE metric = 'resting_heart_rate' AND date(timestamp) = '2026-03-02'"
        assert [r["value"] for r in conn.execute(rhr)] == [49]  # corrected in place, not duplicated
        assert conn.execute("SELECT COUNT(*) FROM measurements").fetchone()[0] == 12
    finally:
        conn.close()
