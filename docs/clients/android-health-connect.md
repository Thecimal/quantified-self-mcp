# Android (Health Connect)

Quantified Self MCP has a `health-connect` importer already built in
(`import_adapters.py`), but there's no Apple-style "Export All Health Data"
button on Android — this page covers the extra step Android needs first.

## Why this isn't one step like Apple Health

Health Connect's own **Settings → Health Connect → Manage data → Backup
and restore** only produces an undocumented raw SQLite snapshot inside a
zip, meant for restoring to another Android device — not a format this
(or any third-party) tool parses. Google Takeout has no Health Connect
entry either. You need an app that reads Health Connect through its
public API and writes out a plain JSON file of records.

## 1. Export from Health Connect to JSON

Any app that exports Health Connect records as JSON works, since the
importer reads the record shape Health Connect's API itself defines
(`recordType`, e.g. `StepsRecord`, `HeartRateRecord`), not something
app-specific. Two options:

- **[Health Data Export](https://play.google.com/store/apps/details?id=com.teqxnology.healthdataexport)**
  (Play Store) — supports JSON export, no account needed.
- **[HealthConnectExports](https://github.com/angeloanan/HealthConnectExports)**
  (open source) — exports to a JSON file over HTTP, if you'd rather run
  something you can read the source of.

Either way, grant the app read permission for the metrics you want
(steps, heart rate, sleep, weight, exercise), then export. You should end
up with either a JSON array of records, or `{"records": [...]}` — both
shapes are accepted.

## 2. Import it

```bash
source .venv/bin/activate
python init_db.py path/to/health-connect-export.json --source health-connect --report
```

`--source health-connect` is required here — `.json` alone isn't enough
for `--source auto` to guess correctly, since this project's own CSV
format doesn't use that extension but other JSON exports might exist in
the future. `--report` (see the main README) shows what was found,
imported, skipped, and left unsupported.

## What's mapped

| Health Connect `recordType`             | Column               |
| ---------------------------------------- | -------------------- |
| `StepsRecord`                            | `steps`               |
| `HeartRateRecord`                        | `heart_rate`          |
| `RestingHeartRateRecord`                 | `resting_heart_rate`  |
| `HeartRateVariabilityRmssdRecord` / `HeartRateVariabilityRecord` | `hrv_ms` |
| `WeightRecord`                           | `weight_kg`           |
| `ExerciseSessionRecord`                  | `workout_minutes`     |
| `SleepSessionRecord`                     | `sleep_hours`         |

Anything else (blood pressure, oxygen saturation, nutrition, etc.) is
counted under "Unsupported" in `--report` output, not silently dropped —
see `import_adapters.py`'s `adapt_health_connect` to add a record type.

## Troubleshooting

- **"doesn't look like a Health Connect export"**: the importer expects
  a JSON array of records, or `{"records": [...]}`. If your export tool
  produced something else (e.g. one file per metric, or CSV), convert or
  pick a different exporter first.
- **Field names don't match**: exporter apps aren't all identical to
  Health Connect's own API field names (`startTime`, `beatsPerMinute`,
  etc.). If `--report` shows 0 imported despite real data, open the JSON
  and compare its keys against `adapt_health_connect` in
  `import_adapters.py` — a small exporter mismatch is the usual cause.
- **Everything shows up as "Unsupported"**: confirm you exported the
  record types listed above specifically (not just "all data" from an
  app that uses different type names for the same metrics).
