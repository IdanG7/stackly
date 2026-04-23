"""Version consistency test."""

import tomllib
from pathlib import Path

import stackly


def test_version_matches_pyproject():
    pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
    with open(pyproject, "rb") as f:
        data = tomllib.load(f)
    assert stackly.__version__ == data["project"]["version"]
