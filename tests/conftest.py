from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from imbalance.calendar import SessionRules  # noqa: E402
from imbalance.config import AppConfig, SessionConfig  # noqa: E402

DAY = date(2026, 11, 18)  # a Wednesday


@pytest.fixture
def day() -> date:
    return DAY


@pytest.fixture
def cfg() -> AppConfig:
    return AppConfig.from_dict({})


@pytest.fixture
def session() -> SessionRules:
    return SessionRules(SessionConfig())
