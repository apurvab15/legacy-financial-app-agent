"""Shared fixtures. These tests touch no LLM, no network and no browser."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return REPO_ROOT


@pytest.fixture(scope="session")
def capability_paths() -> list[Path]:
    return sorted((REPO_ROOT / "capabilities").glob("*.json"))
