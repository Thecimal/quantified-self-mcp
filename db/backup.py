"""Consistent backup and safe restore of the health database.

Copying health.db with cp/rsync is not a backup: the database runs in WAL mode, so recent (and, for a new
database, all) committed data can still be in health.db-wal, and a copy of health.db alone then holds
nothing or an older state. This module uses SQLite's own online-backup API instead, which takes a consistent
snapshot even while the MCP server is running, and writes it as one self-contained file.

    quantified-self-backup create BACKUP_FILE
    quantified-self-backup restore BACKUP_FILE

Both honour --db-path / HEALTH_DB_PATH (and HEALTH_DB_PASSPHRASE for an encrypted database, in which case the
backup is encrypted with the same passphrase) exactly like the importer and the server. This is deliberately
not an MCP tool: restoring replaces the whole database, which a model must never be able to trigger.

A backup is staged next to its destination, integrity-checked, and only then renamed into place, so a failed
or interrupted backup never leaves a partial file under the real name and an existing backup is never
overwritten. A restore validates the backup first (SQLite integrity check, it is a health database, and its
schema is not newer than this release understands), stages and re-checks a copy, and only then moves the
current database aside, as <name>.pre-restore-<UTC time>, rather than deleting it. If any step fails the
current database stays in place with its contents unchanged.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from logic import SCHEMA_VERSION, connect_writable, db_error_types, default_data_dir, readonly_connection

_SIDECARS = ("", "-wal", "-shm")  # a SQLite database in WAL mode is the main file plus these two
_APP_TABLES = frozenset({"measurements", "daily_metrics"})


class BackupError(Exception):
    """A problem the user can act on; the message says what happened and what state everything is in."""


def default_db_path() -> Path:
    base_dir = Path(__file__).resolve().parent.parent
    return Path(os.environ.get("HEALTH_DB_PATH", default_data_dir(base_dir) / "health.db")).expanduser()


def _remove_database_files(path: Path) -> None:
    for suffix in _SIDECARS:
        Path(str(path) + suffix).unlink(missing_ok=True)


def _check_database(path: Path) -> dict:
    """Validate that `path` is an intact health database this release can use. Returns facts about it
    (schema version, measurement count) for comparing a copy with its original. Opens it read-only."""
    if not path.is_file():
        raise BackupError(f"{path} does not exist or is not a file.")
    try:
        with readonly_connection(path) as conn:
            problems = [row[0] for row in conn.execute("PRAGMA integrity_check").fetchall()]
            if problems != ["ok"]:
                raise BackupError(f"{path.name} failed SQLite's integrity check ({problems[0]}); it is damaged.")
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            count = conn.execute("SELECT COUNT(*) FROM measurements").fetchone()[0] if "measurements" in tables else 0
    except BackupError:
        raise
    except (RuntimeError, *db_error_types()) as exc:
        raise BackupError(
            f"{path.name} is not a readable health database ({exc}). If your database is encrypted, "
            "set HEALTH_DB_PASSPHRASE to the passphrase it was created with."
        ) from exc
    if not tables & _APP_TABLES:
        raise BackupError(f"{path.name} is a SQLite file but not a Quantified Self health database.")
    if version > SCHEMA_VERSION:
        raise BackupError(
            f"{path.name} has schema version {version}, newer than this release understands ({SCHEMA_VERSION}). "
            "Upgrade quantified-self-mcp before using it."
        )
    return {"user_version": version, "measurements": count}


def _snapshot(source: Path, target: Path) -> None:
    """Copy `source` into a new file `target` with SQLite's online-backup API, as one self-contained file."""
    with readonly_connection(source) as src:
        out = connect_writable(target)
        try:
            src.backup(out)
            out.execute("PRAGMA journal_mode = DELETE")  # no -wal/-shm beside the copy
        finally:
            out.close()


def create_backup(db_path: Path, dest: Path) -> dict:
    db_path, dest = Path(db_path), Path(dest)
    if not db_path.is_file():
        raise BackupError(f"There is no database at {db_path}, so there is nothing to back up.")
    if dest.exists():
        raise BackupError(f"{dest} already exists. A backup is never overwritten; choose a new file name.")
    dest.parent.mkdir(parents=True, exist_ok=True)
    staged = dest.with_name(f".{dest.name}.partial-{os.getpid()}")
    try:
        try:
            _snapshot(db_path, staged)
        except (RuntimeError, *db_error_types()) as exc:
            raise BackupError(f"Could not read the database at {db_path} ({exc}); no backup was written.") from exc
        facts = _check_database(staged)
        os.chmod(staged, 0o600)  # health data: owner-only, whatever the umask
        os.replace(staged, dest)
    finally:
        _remove_database_files(staged)
    return facts


def _require_unused(db_path: Path) -> None:
    """Refuse to replace a database another process has open: SQLite would otherwise apply the old
    database's write-ahead log to the restored file. Also leaves the current database checkpointed, as a
    single file, so the copy kept aside is complete. A database that cannot be opened at all (damaged) is
    simply moved aside by the caller."""
    try:
        conn = connect_writable(db_path)
    except (RuntimeError, *db_error_types()):
        return
    try:
        conn.execute("PRAGMA locking_mode = EXCLUSIVE")
        conn.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()  # takes the exclusive lock
        conn.execute("PRAGMA journal_mode = DELETE")
    except db_error_types() as exc:
        if "lock" in str(exc).lower() or "busy" in str(exc).lower():
            raise BackupError(
                f"{db_path} is in use by another program (for example an MCP client running the server). "
                "Close it and try again; nothing was changed."
            ) from exc
        # any other failure: the file is damaged, which is what a restore is for
    finally:
        conn.close()


def restore_backup(backup_path: Path, db_path: Path) -> dict:
    backup_path, db_path = Path(backup_path), Path(db_path)
    facts = _check_database(backup_path)  # nothing is touched until the backup is known to be good
    db_path.parent.mkdir(parents=True, exist_ok=True)
    staged = db_path.with_name(f".{db_path.name}.restore-{os.getpid()}")
    aside: Path | None = None
    try:
        try:
            _snapshot(backup_path, staged)
        except (RuntimeError, *db_error_types()) as exc:
            raise BackupError(f"Could not read {backup_path.name} ({exc}); nothing was changed.") from exc
        if _check_database(staged) != facts:
            raise BackupError("The staged copy does not match the backup; nothing was changed.")
        if any(Path(str(db_path) + suffix).exists() for suffix in _SIDECARS):
            _require_unused(db_path)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            aside = db_path.with_name(f"{db_path.name}.pre-restore-{stamp}")
            attempt = 1
            while any(Path(str(aside) + suffix).exists() for suffix in _SIDECARS):  # never reuse an earlier one
                attempt += 1
                aside = db_path.with_name(f"{db_path.name}.pre-restore-{stamp}-{attempt}")
            moved: list[str] = []
            try:
                for suffix in _SIDECARS:
                    current = Path(str(db_path) + suffix)
                    if current.exists():
                        os.replace(current, Path(str(aside) + suffix))
                        moved.append(suffix)
                os.replace(staged, db_path)
            except OSError as exc:
                for suffix in moved:  # put the previous database back exactly as it was
                    os.replace(Path(str(aside) + suffix), Path(str(db_path) + suffix))
                raise BackupError(f"Could not replace {db_path} ({exc}); the previous database was kept.") from exc
        else:
            os.replace(staged, db_path)
    finally:
        _remove_database_files(staged)
    return {**facts, "previous_database": str(aside) if aside else None}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="quantified-self-backup",
        description="Create a consistent backup of the health database, or safely restore one.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    for name, help_text in (
        ("create", "write a consistent backup of the database to a new file (never overwrites)"),
        ("restore", "replace the database with a validated backup, keeping the current one aside"),
    ):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("backup_file", type=Path)
        p.add_argument(
            "--db-path", type=Path, default=None, help="database file (default: HEALTH_DB_PATH or the usual location)"
        )
    args = parser.parse_args(argv)
    db_path = args.db_path if args.db_path is not None else default_db_path()
    try:
        if args.command == "create":
            facts = create_backup(db_path, args.backup_file)
            print(
                f"Backup written to {args.backup_file}: {facts['measurements']} measurement(s), "
                f"schema v{facts['user_version']}."
            )
        else:
            facts = restore_backup(args.backup_file, db_path)
            print(f"Restored {db_path}: {facts['measurements']} measurement(s), schema v{facts['user_version']}.")
            if facts["previous_database"]:
                print(f"The previous database was kept at {facts['previous_database']}.")
    except BackupError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
