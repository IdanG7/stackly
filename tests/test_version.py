"""Version consistency test."""

import tomllib
from pathlib import Path

import debugbridge


def test_version_matches_pyproject():
    pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
    with open(pyproject, "rb") as f:
        data = tomllib.load(f)
    assert debugbridge.__version__ == data["project"]["version"]
