"""Shared fixtures: a scaled-down config and the tables it generates."""

from __future__ import annotations

import copy
from pathlib import Path

import pandas as pd
import pytest

from src import generate
from src.clean import clean_all
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
    c["lookalikes"]["community_collection"]["customers"] = 8
    c["lookalikes"]["steady_trader"]["customers"] = 6
    c["lookalikes"]["family_shared_phone"]["groups"] = 5
    c["us_outbound"]["senders"] = 600
    c["us_outbound"]["planted"] = {"structuring": 3, "collector": 2}
    return c


@pytest.fixture(scope="session")
def tables(cfg: dict) -> dict[str, pd.DataFrame]:
    """All generated tables for the small config."""
    return generate.build(cfg)


@pytest.fixture(scope="session")
def raw_dir(tables: dict, tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Generated files written to disk (full, upload, eval)."""
    out = tmp_path_factory.mktemp("raw")
    generate.write(tables, out)
    return out


@pytest.fixture(scope="session")
def cleaned(raw_dir: Path, cfg: dict) -> dict[str, pd.DataFrame]:
    """S1-cleaned tables for the small config."""
    return clean_all(raw_dir / "upload", cfg)[0]
