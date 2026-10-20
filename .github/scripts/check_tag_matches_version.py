"""Refuse to release when the git tag and the package version disagree.

Usage: python .github/scripts/check_tag_matches_version.py <tag> [path/to/pyproject.toml]

The publish workflow runs this on the pushed tag (for example ``v0.4.2``) before anything is built or
uploaded. The tag must be exactly ``v`` plus the ``[project]`` version in pyproject.toml, so an artifact
on PyPI can always be traced back to the tag, and therefore the commit, that produced it.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path


def project_version(pyproject_text: str) -> str:
    in_project = False
    for line in pyproject_text.splitlines():
        stripped = line.strip()
        if stripped.startswith("["):
            in_project = stripped == "[project]"
            continue
        if in_project:
            match = re.fullmatch(r'version\s*=\s*"([^"]+)"', stripped)
            if match:
                return match.group(1)
    raise ValueError('no version = "..." entry found in the [project] table')


def main(argv: list[str]) -> int:
    if not argv or len(argv) > 2:
        print(__doc__, file=sys.stderr)
        return 2
    tag = argv[0]
    pyproject = Path(argv[1] if len(argv) == 2 else "pyproject.toml")
    try:
        version = project_version(pyproject.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"Cannot read the package version from {pyproject}: {exc}", file=sys.stderr)
        return 2
    expected = f"v{version}"
    if tag != expected:
        print(
            f"Tag {tag!r} does not match the package version {version!r} in {pyproject}: expected tag {expected!r}. "
            "Bump the version in pyproject.toml (and the changelog) before tagging, or tag the right commit.",
            file=sys.stderr,
        )
        return 1
    print(f"Tag {tag} matches package version {version}.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
