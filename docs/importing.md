# Importing Health Data

Quantified Self MCP stores your health history in a local SQLite database (`data/health.db`).
You can import data from CSV exports, Apple Health, or Android Health Connect using the `quantified-self-init-db` command-line utility (or `python init_db.py`).

## Quick Start

```bash
# From CSV
quantified-self-init-db export.csv

# From Apple Health
quantified-self-init-db export.xml

# From Android Health Connect JSON
quantified-self-init-db health-connect.json --source health-connect
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

---

## Import Options & CLI Flags

| Flag | Description |
| --- | --- |
| `--source {auto,csv,apple-health,health-connect}` | Explicitly declare format. Defaults to auto-detection from file extension (`.xml` -> `apple-health`, otherwise `csv`). |
| `--replace` | Wipe existing tables before importing instead of incrementally appending/upserting. |
| `--report` | Print detailed import metrics: date range, records imported, skipped records, and unmapped types. |
| `--map COLUMN=HEADER` | Rename CSV columns to standard schema names (repeatable). |
| `--db-path DB_PATH` | Path to destination SQLite database (defaults to `$HEALTH_DB_PATH` or `data/health.db`). |

## Incremental Imports

Running `quantified-self-init-db` without `--replace` performs an incremental upsert:
* New dates and raw observations are merged with existing records.
* Each import run is recorded in the `imports` table with timestamps, row counts, and error tracking.
* Check past imports at any time via the MCP `get_import_status` tool or CLI.
