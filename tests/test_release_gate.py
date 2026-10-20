"""The release pipeline must not publish anything that CI has not accepted.

Version 0.4.0 reached PyPI with a wheel that was missing db/, tools/ and most modules, so `import server`
failed for everyone who installed it. These tests pin the gates that keep that from happening again: the tag
must match the package version, the full CI workflow must pass first, and the wheel that is uploaded must be
installed and smoke-tested before the upload step runs.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / ".github" / "scripts" / "check_tag_matches_version.py"
PUBLISH = REPO / ".github" / "workflows" / "publish.yml"
CI = REPO / ".github" / "workflows" / "ci.yml"

PYPROJECT = (
    '[build-system]\nrequires = ["hatchling"]\n\n'
    '[project]\nname = "x"\nversion = "1.2.3"\n\n'
    '[tool.other]\nversion = "9.9.9"\n'
)


def _triggers(workflow: dict) -> dict:
    # PyYAML (YAML 1.1) reads the bare key `on` as the boolean True.
    return workflow.get("on") or workflow[True]


def _check(tmp_path, tag, text=PYPROJECT):
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(text)
    return subprocess.run(
        [sys.executable, str(SCRIPT), tag, str(pyproject)], capture_output=True, text=True, timeout=60
    )


def test_matching_tag_passes(tmp_path):
    result = _check(tmp_path, "v1.2.3")
    assert result.returncode == 0, result.stderr


def test_mismatched_tag_is_refused_with_both_values_named(tmp_path):
    result = _check(tmp_path, "v1.2.4")
    assert result.returncode == 1
    assert "v1.2.4" in result.stderr and "1.2.3" in result.stderr


def test_tag_without_v_prefix_is_refused(tmp_path):
    # The repository has a bare `0.4.0` tag, which the publish trigger (`v*`) never matched.
    assert _check(tmp_path, "1.2.3").returncode == 1


def test_only_the_project_table_version_counts(tmp_path):
    assert _check(tmp_path, "v9.9.9").returncode == 1


def test_unreadable_version_is_an_error_not_a_pass(tmp_path):
    result = _check(tmp_path, "v1.2.3", text="[project]\nname = 'x'\n")
    assert result.returncode == 2


def test_the_real_pyproject_version_is_accepted_for_its_own_tag():
    sys.path.insert(0, str(SCRIPT.parent))
    try:
        import check_tag_matches_version as mod
    finally:
        sys.path.remove(str(SCRIPT.parent))
    version = mod.project_version((REPO / "pyproject.toml").read_text())
    result = subprocess.run(
        [sys.executable, str(SCRIPT), f"v{version}", str(REPO / "pyproject.toml")], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr


def test_ci_can_be_called_by_the_publish_workflow():
    assert "workflow_call" in _triggers(yaml.safe_load(CI.read_text()))


def test_publish_runs_only_for_v_tags():
    assert _triggers(yaml.safe_load(PUBLISH.read_text())) == {"push": {"tags": ["v*"]}}


def test_publish_is_gated_on_tag_check_and_full_ci():
    jobs = yaml.safe_load(PUBLISH.read_text())["jobs"]
    assert "check_tag_matches_version.py" in "".join(s.get("run", "") for s in jobs["verify-tag"]["steps"])
    assert jobs["ci"]["uses"] == "./.github/workflows/ci.yml"
    assert jobs["ci"]["needs"] == "verify-tag"
    assert jobs["publish"]["needs"] == "ci"


def test_publish_smoke_tests_the_exact_wheel_before_uploading():
    steps = yaml.safe_load(PUBLISH.read_text())["jobs"]["publish"]["steps"]
    runs = [s.get("run", "") for s in steps]
    uses = [s.get("uses", "") for s in steps]
    build = next(i for i, r in enumerate(runs) if "python -m build" in r)
    smoke = next(i for i, r in enumerate(runs) if "QS_SMOKE_WHEEL" in r and "test_clean_install_smoke" in r)
    upload = next(i for i, u in enumerate(uses) if u.startswith("pypa/gh-action-pypi-publish"))
    assert build < smoke < upload
    assert "QS_SMOKE_ISOLATED=1" in runs[smoke]  # dependencies come from the index, as for a real user
