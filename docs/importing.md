# Importing Health Data

Quantified Self MCP stores your health history in a local SQLite database (`data/health.db`).
You can import data from CSV exports, Apple Health, Android Health Connect, or WHOOP API JSON using the `quantified-self-init-db` command-line utility (or `python init_db.py`).

## Quick Start

```bash
# From CSV
quantified-self-init-db export.csv

# From Apple Health
quantified-self-init-db export.xml

# From Android Health Connect JSON
quantified-self-init-db health-connect.json --source health-connect

# From WHOOP Developer API JSON
quantified-self-init-db whoop.json --source whoop
```

## Supported Import Formats

### 1. CSV (`--source csv`)

By default, `.csv` files are expected to have a `date` column (format `YYYY-MM-DD`) and one or more metric columns:

* `steps` (count)
* `sleep_hours` (hours)
* `resting_heart_rate` (bpm)
* `heart_rate` (bpm)
* `hrv_ms` (ms)
* `weight_kg` (kg)
* `workout_minutes` (minutes)
* `mood` (scale 1–10)
* `water_ml` (mL)

#### Column Mapping (`--map`)

If your CSV uses different column headers, map them without modifying the source file:

```bash
quantified-self-init-db my_fitbit_data.csv \
  --map date="Date" \
  --map steps="Daily Steps" \
  --map sleep_hours="Hours Slept" \
  --map resting_heart_rate="Resting HR"
```

### 2. Apple Health (`--source apple-health`)

Export your health data directly from your iPhone:
1. Open the **Health** app on iOS.
2. Tap your profile icon in the top right.
3. Tap **Export All Health Data**.
4. Unzip the downloaded `export.zip` and locate `export.xml`.
5. Import it:
   ```bash
   quantified-self-init-db export.xml --report
   ```

### 3. Android Health Connect (`--source health-connect`)

Health Connect records exported to JSON via apps like *Health Data Export* can be imported directly:

```bash
quantified-self-init-db health-connect.json --source health-connect --report
```

See [Android Health Connect Setup Guide](clients/android-health-connect.md) for step-by-step instructions.

### 4. WHOOP Developer API JSON (`--source whoop`)

JSON saved from the [WHOOP Developer API](https://developer.whoop.com/api) (v2): a JSON array of records, or one
`{"records": [...]}` collection response. Records from `/v2/recovery`, `/v2/activity/sleep` and
`/v2/activity/workout` can be concatenated into one array; each record is recognised by its own shape.
`--source whoop` is required (a `.json` file is never auto-detected).

```bash
quantified-self-init-db whoop.json --source whoop --report
```

This is **not** the CSV export in the WHOOP app, whose column headers are not documented and are not parsed.
The adapter was written against the sample responses in WHOOP's v2 API documentation and tested only on
synthetic data built from them (`sample_data/whoop_sample.json`); it has not been tried against a real
account, and other API versions are not covered.

| Metric | Read from | Rule |
| --- | --- | --- |
| `sleep_hours` | sleep `score.stage_summary` | light + slow-wave + REM time, in hours (awake and in-bed time not counted); naps are included; needs `score_state` `SCORED`; counted toward the local day you woke up |
| `resting_heart_rate` | recovery `score.resting_heart_rate` | needs `SCORED`; dated by the sleep it points to, which must be in the same file |
| `hrv_ms` | recovery `score.hrv_rmssd_milli` | an **RMSSD**, not Apple Health's SDNN, so the two are not directly comparable |
| `workout_minutes` | workout `end` minus `start` | counted toward the local start day; needs no score |

Each value is kept as its own observation with the source `WHOOP`, so it can be told apart from other devices.
Local time is the record's UTC time plus its `timezone_offset`. A record repeated in the file is imported once,
and the result does not depend on record order. A record that cannot be read (bad timestamp, missing field, value
out of range, recovery whose sleep is absent) is skipped and counted, and the rest still import. Records that are
not `SCORED` and anything else in the file (cycles, strain, SpO2, skin temperature, respiratory rate, heart-rate
zones, sport type, energy) are listed under "Unsupported" by `--report` and are not stored.

---

## Import Options & CLI Flags

| Flag | Description |
| --- | --- |
| `--source {auto,csv,apple-health,health-connect,whoop}` | Explicitly declare format. Defaults to auto-detection from file extension (`.xml` -> `apple-health`, otherwise `csv`). |
| `--replace` | Wipe existing tables before importing instead of incrementally appending/upserting. |
| `--report` | Print detailed import metrics: date range, records imported, skipped records, and unmapped types. |
| `--map COLUMN=HEADER` | Rename CSV columns to standard schema names (repeatable). |
| `--db-path DB_PATH` | Path to destination SQLite database (defaults to `$HEALTH_DB_PATH` or `data/health.db`). |

## Incremental Imports

Running `quantified-self-init-db` without `--replace` performs an incremental upsert:
* New dates and raw observations are merged with existing records.
* Each import run is recorded in the `imports` table with timestamps, row counts, and error tracking.
* Check past imports at any time via the MCP `get_import_status` tool or CLI.
