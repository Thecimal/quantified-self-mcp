"""quantified-self-doctor: environment + data diagnostic for onboarding.

Run after install to check every prerequisite before connecting an MCP
client, and to get a concrete "next step" instead of a wall of setup docs.
"""
from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path

CHECK = "\u2713"
CROSS = "\u2717"
WARN = "!"

REQUIRED_HEALTH_COLUMNS = {"date", "steps", "sleep_hours", "resting_heart_rate"}
OPTIONAL_HEALTH_COLUMNS = {"weight_kg", "workout_minutes", "mood", "water_ml"}

DATA_DIR = Path("data")
HEALTH_DB = DATA_DIR / "health.db"


def _ok(msg: str) -> None:
    print(f"{CHECK} {msg}")


def _fail(msg: str) -> None:
    print(f"{CROSS} {msg}")


def _warn(msg: str) -> None:
    print(f"{WARN} {msg}")


def check_python() -> bool:
    # ruff's UP036 assumes this comparison is dead code given
    # requires-python = ">=3.10" in pyproject.toml, but that constraint
    # only applies to `pip install`-managed environments — this script is
    # also run directly (e.g. a stray `python3 doctor.py` from a system
    # interpreter pip never touched), which is exactly the case this
    # check exists to catch.
    if sys.version_info >= (3, 10):  # noqa: UP036
        _ok(f"Python {sys.version_info.major}.{sys.version_info.minor} installed")
        return True
    _fail(f"Python {sys.version_info.major}.{sys.version_info.minor} found, 3.10+ required")
    return False


def check_mcp_server() -> bool:
    if importlib.util.find_spec("fastmcp") is not None or importlib.util.find_spec("mcp") is not None:
        _ok("MCP server available")
        return True
    _fail("MCP server not installed — run: pip install quantified-self-mcp")
    return False


def check_database() -> sqlite3.Connection | None:
    if not HEALTH_DB.exists():
        _fail("Database not found — run: quantified-self-init-db <your-file>")
        return None
    _ok("Database found")
    try:
        return sqlite3.connect(f"file:{HEALTH_DB}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        _fail(f"Database could not be opened: {exc}")
        return None


def check_days(conn: sqlite3.Connection) -> int:
    try:
        (count,) = conn.execute("SELECT COUNT(*) FROM health").fetchone()
    except sqlite3.Error:
        _fail("Database found, but has no 'health' table — re-run quantified-self-init-db")
        return 0
    if count == 0:
        _warn("Database contains 0 days — import a CSV or Apple Health export")
    else:
        _ok(f"Database contains {count} days")
    return count


def check_metrics(conn: sqlite3.Connection) -> int:
    cols = {row[1] for row in conn.execute("PRAGMA table_info(health)").fetchall()}
    populated = set()
    for col in (REQUIRED_HEALTH_COLUMNS | OPTIONAL_HEALTH_COLUMNS) & cols:
        (non_null,) = conn.execute(
            f"SELECT COUNT(*) FROM health WHERE {col} IS NOT NULL"
        ).fetchone()
        if non_null > 0:
            populated.add(col)
    if populated:
        _ok(f"{len(populated)} metrics available ({', '.join(sorted(populated))})")
    else:
        _warn("0 metrics have data yet")
    return len(populated)


def check_apple_health_import() -> None:
    try:
        import import_adapters  # noqa: F401
    except ImportError:
        _warn("Apple Health import adapter not found (optional)")
        return
    _ok("Apple Health import valid")


def suggest_next_step(python_ok: bool, mcp_ok: bool, db_ok: bool, days: int, metrics: int) -> None:
    print()
    print("Next step:")
    if not python_ok:
        print("Install Python 3.10 or newer")
    elif not mcp_ok:
        print("pip install quantified-self-mcp")
    elif not db_ok or days == 0:
        print("quantified-self-init-db sample_data/health_sample.csv   (or your own export)")
    elif metrics == 0:
        print("Re-import your data — the database exists but has no values yet")
    else:
        print("Connect to Claude Desktop (or any MCP-compatible client) and ask a question")


def main() -> int:
    print("Running quantified-self-doctor...\n")
    python_ok = check_python()
    mcp_ok = check_mcp_server()
    conn = check_database()
    days = metrics = 0
    if conn is not None:
        days = check_days(conn)
        metrics = check_metrics(conn)
        check_apple_health_import()
        conn.close()
    suggest_next_step(python_ok, mcp_ok, conn is not None, days, metrics)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
