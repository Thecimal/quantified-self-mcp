"""Clean-install smoke test: built wheel -> fresh venv -> documented commands -> MCP result.

Nothing here imports the project from the checkout. The wheel is built from the
repository, installed into a brand-new virtualenv, and every step runs through the
console scripts the README documents:

    quantified-self-init-db <csv>   (initialize + import)
    quantified-self-doctor          (documented verification step)
    quantified-self-mcp             (started over stdio and queried with an MCP client)

By default the venv reuses the test runner's already-installed third-party packages
(so the test needs no network beyond building the wheel). Set QS_SMOKE_ISOLATED=1 to
install the wheel's declared dependencies from the package index instead, which is what
catches a missing or wrong runtime dependency declaration.

Set QS_SMOKE_WHEEL=<path to a .whl> to test an already-built wheel instead of building one; the
publish workflow uses this so the file that is uploaded to PyPI is the file that was smoke-tested.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
import venv
import zipfile
from pathlib import Path

import pytest
from fastmcp import Client
from fastmcp.client.transports import StdioTransport

REPO = Path(__file__).resolve().parent.parent
SAMPLE_CSV = REPO / "sample_data" / "health_sample.csv"
ISOLATED = os.environ.get("QS_SMOKE_ISOLATED") == "1"
PREBUILT_WHEEL = os.environ.get("QS_SMOKE_WHEEL")


def _run(cmd, **kwargs):
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=600, **kwargs)
    assert result.returncode == 0, f"{cmd} failed ({result.returncode}):\n{result.stdout}\n{result.stderr}"
    return result


def _clean_env(db_path: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in {"PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"}}
    env["HEALTH_DB_PATH"] = str(db_path)
    env["FASTMCP_CHECK_FOR_UPDATES"] = "off"
    return env


def _build_wheel(work: Path) -> Path:
    if importlib.util.find_spec("build") is None:
        pytest.skip("the 'build' package is not installed (pip install -r requirements-dev.txt)")
    # Build from a copy so the checkout never gains build/ or *.egg-info side effects.
    src = work / "src"
    shutil.copytree(
        REPO,
        src,
        ignore=shutil.ignore_patterns(".git", ".venv", "venv", "dist", "build", "__pycache__", "*.egg-info", "data"),
    )
    dist = work / "dist"
    build_cmd = [sys.executable, "-m", "build", "--wheel", "--outdir", str(dist), str(src)]
    if importlib.util.find_spec("hatchling") is not None:
        build_cmd.insert(4, "--no-isolation")
    _run(build_cmd)
    return next(dist.glob("*.whl"))


@pytest.fixture(scope="module")
def installed(tmp_path_factory):
    work = tmp_path_factory.mktemp("clean_install")
    if PREBUILT_WHEEL:
        wheel = Path(PREBUILT_WHEEL).resolve()
        assert wheel.is_file(), f"QS_SMOKE_WHEEL does not point at a wheel file: {wheel}"
    else:
        wheel = _build_wheel(work)

    env_dir = work / "venv"
    venv.EnvBuilder(with_pip=True, clear=True).create(env_dir)
    bindir = env_dir / ("Scripts" if os.name == "nt" else "bin")
    exe = ".exe" if os.name == "nt" else ""
    python = bindir / f"python{exe}"

    if ISOLATED:
        _run([str(python), "-m", "pip", "install", "--disable-pip-version-check", str(wheel)])
    else:
        _run(
            [str(python), "-m", "pip", "install", "--disable-pip-version-check", "--no-deps", "--no-index", str(wheel)]
        )
        purelib = _run([str(python), "-c", "import sysconfig; print(sysconfig.get_paths()['purelib'])"]).stdout.strip()
        runner_paths = {p for p in sys.path if p.endswith("site-packages") and Path(p).is_dir()}
        (Path(purelib) / "runner_site_packages.pth").write_text("\n".join(sorted(runner_paths)) + "\n")

    return {"wheel": wheel, "bindir": bindir, "exe": exe, "python": python, "work": work}


def test_wheel_declares_runtime_dependencies(installed):
    with zipfile.ZipFile(installed["wheel"]) as zf:
        metadata = next(zf.read(n).decode() for n in zf.namelist() if n.endswith(".dist-info/METADATA"))
    requires = {
        line.split(":", 1)[1].strip().split(">")[0].split("<")[0].split("=")[0].strip().lower()
        for line in metadata.splitlines()
        if line.startswith("Requires-Dist:") and "extra ==" not in line
    }
    assert {"fastmcp", "pydantic", "pyyaml"} <= requires


async def test_install_init_import_doctor_and_mcp_query(installed):
    work, bindir, exe, python = installed["work"], installed["bindir"], installed["exe"], installed["python"]
    db_path = work / "state" / "health.db"
    db_path.parent.mkdir()
    env = _clean_env(db_path)
    run_dir = work / "run"
    run_dir.mkdir()
    csv_path = run_dir / "health_sample.csv"
    shutil.copy(SAMPLE_CSV, csv_path)

    # The modules must come from the installed wheel, not from the checkout.
    where = _run(
        [str(python), "-I", "-c", "import server, init_db; print(server.__file__); print(init_db.__file__)"],
        cwd=run_dir,
        env=env,
    ).stdout.split()
    assert all(Path(p).is_relative_to(installed["work"] / "venv") for p in where), where

    # Initialize + import through the documented entry point.
    init = _run([str(bindir / f"quantified-self-init-db{exe}"), str(csv_path)], cwd=run_dir, env=env)
    assert db_path.exists(), init.stdout + init.stderr
    assert "30 row(s) loaded" in init.stdout

    # The documented verification step must recognise the database that init just created.
    doctor = _run([str(bindir / f"quantified-self-doctor{exe}")], cwd=run_dir, env=env)
    assert "Database found" in doctor.stdout, doctor.stdout
    assert "✗" not in doctor.stdout, doctor.stdout
    assert "Database contains 30 days" in doctor.stdout, doctor.stdout

    # Start the server through its documented entry point and make real MCP requests.
    transport = StdioTransport(command=str(bindir / f"quantified-self-mcp{exe}"), args=[], env=env, cwd=str(run_dir))
    async with Client(transport) as client:
        assert len(await client.list_tools()) >= 20
        status = await client.call_tool("get_data_status", {})
        assert status.structured_content["days_with_data"] == 30
        assert status.structured_content["status"] in {"CURRENT", "STALE"}
        rows = await client.call_tool("read_measurements", {"metric": "steps", "limit": 5})
        assert rows.structured_content["count"] == 5
        assert all(m["metric"] == "steps" for m in rows.structured_content["measurements"])
