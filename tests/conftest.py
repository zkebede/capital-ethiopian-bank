"""Shared fixtures: a scaled-down config and the tables it generates."""

from __future__ import annotations

import copy

import pandas as pd
import pytest

from src import generate
from src.common import load_config


@pytest.fixture(scope="session")
def cfg() -> dict:
    """Small config: same parameters, fewer rows and planted cases."""
    c = copy.deepcopy(load_config())
    c["rows"] = {
        "customers": 3000,
        "remittances": 20000,
        "wallet_txns": 10000,
        "fx_conversions": 1000,
        "alerts": 1000,
        "us_outbound": 2000,
    }
    c["planted"] = {
        "structuring": 10,
        "collector": 5,
        "pass_through": 10,
        "mule_rings": 3,
        "fx_out_of_profile": 8,
        "duplicate_profile_pairs": 10,
    }
    c["us_outbound"]["senders"] = 600
    c["us_outbound"]["planted"] = {"structuring": 3, "collector": 2}
    return c


@pytest.fixture(scope="session")
def tables(cfg: dict) -> dict[str, pd.DataFrame]:
    """All generated tables for the small config."""
    return generate.build(cfg)
