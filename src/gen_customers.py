"""Generate the customer master file (before any typology is planted)."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from src.common import NONE, ethiopian_names, rng_for, window, zero_pad_ids

# Fictional branch network. Regions are descriptive reference data only and are
# never used as a risk signal anywhere in this project.
BRANCHES: dict[str, tuple[str, str, float]] = {
    # branch: (region, city, weight)
    "Bole Branch": ("Addis Ababa", "Addis Ababa", 0.07),
    "Piassa Branch": ("Addis Ababa", "Addis Ababa", 0.05),
    "Merkato Branch": ("Addis Ababa", "Addis Ababa", 0.06),
    "Kazanchis Branch": ("Addis Ababa", "Addis Ababa", 0.04),
    "CMC Branch": ("Addis Ababa", "Addis Ababa", 0.03),
    "Adama Branch": ("Oromia", "Adama", 0.07),
    "Jimma Branch": ("Oromia", "Jimma", 0.06),
    "Shashemene Branch": ("Oromia", "Shashemene", 0.06),
    "Bishoftu Branch": ("Oromia", "Bishoftu", 0.06),
    "Bahir Dar Branch": ("Amhara", "Bahir Dar", 0.07),
    "Gondar Branch": ("Amhara", "Gondar", 0.06),
    "Dessie Branch": ("Amhara", "Dessie", 0.05),
    "Mekelle Branch": ("Tigray", "Mekelle", 0.06),
    "Hawassa Branch": ("Sidama", "Hawassa", 0.05),
    "Arba Minch Branch": ("South Ethiopia", "Arba Minch", 0.04),
    "Dire Dawa Branch": ("Dire Dawa", "Dire Dawa", 0.04),
    "Harar Branch": ("Harari", "Harar", 0.02),
    "Jigjiga Branch": ("Somali", "Jigjiga", 0.03),
    "Semera Branch": ("Afar", "Semera", 0.02),
    "Assosa Branch": ("Benishangul-Gumuz", "Assosa", 0.01),
    "Gambela Branch": ("Gambela", "Gambela", 0.01),
}

ID_TYPES = {"National ID": 0.45, "Kebele ID": 0.35, "Passport": 0.12, "Driver's License": 0.08}
ID_VERIFIED_SHARE = 0.93
EARLIEST_ONBOARD = pd.Timestamp("2012-01-01")


def _weighted(rng: np.random.Generator, options: dict[str, float], size: int) -> np.ndarray:
    keys = list(options)
    p = np.array([options[k] for k in keys], dtype=float)
    return rng.choice(keys, size=size, p=p / p.sum())


def occupation_bands(cfg: dict[str, Any]) -> dict[str, str]:
    """Map each configured occupation to its income band."""
    return {occ: spec["band"] for occ, spec in cfg["occupations"].items()}


def generate(cfg: dict[str, Any]) -> pd.DataFrame:
    """Return ``rows.customers`` customers, all labelled ``ground_truth = none``.

    Phones and addresses are unique by construction, so any shared identifier in
    the final file was planted on purpose (mule rings, duplicate profiles).
    """
    rng = rng_for(cfg, "customers")
    win = window(cfg)
    n = int(cfg["rows"]["customers"])

    branch_names = list(BRANCHES)
    branch = _weighted(rng, {b: BRANCHES[b][2] for b in branch_names}, n)
    region = np.array([BRANCHES[b][0] for b in branch])
    city = np.array([BRANCHES[b][1] for b in branch])

    age_days = rng.integers(18 * 365, 80 * 365, size=n)
    dob = win.start - pd.to_timedelta(age_days, unit="D")

    phone = np.char.add("+2519", np.char.zfill(rng.choice(10**8, n, replace=False).astype(str), 8))
    house = rng.choice(np.arange(1, 10**6), n, replace=False)
    kebele = rng.integers(1, 25, size=n)
    address = (
        "Kebele "
        + pd.Series(kebele).astype(str).str.zfill(2)
        + ", House No. "
        + pd.Series(house).astype(str)
        + ", "
        + pd.Series(city)
    )

    occ_weights = {occ: spec["weight"] for occ, spec in cfg["occupations"].items()}
    adult_since = dob + pd.DateOffset(years=18)
    lo = np.maximum(adult_since.values, np.datetime64(EARLIEST_ONBOARD))
    span = (win.start - pd.Timedelta(days=1)).to_datetime64() - lo
    onboard = pd.DatetimeIndex(lo + (span * rng.random(n)).astype("timedelta64[D]"))

    return pd.DataFrame(
        {
            "customer_id": zero_pad_ids("C", np.arange(1, n + 1), 6),
            "full_name": ethiopian_names(rng, n),
            "dob": dob.strftime("%Y-%m-%d"),
            "phone": phone,
            "address": address.to_numpy(),
            "branch": branch,
            "region": region,
            "id_type": _weighted(rng, ID_TYPES, n),
            "id_verified": rng.random(n) < ID_VERIFIED_SHARE,
            "occupation": _weighted(rng, occ_weights, n),
            "onboard_date": onboard.strftime("%Y-%m-%d"),
            "diaspora_flag": rng.random(n) < float(cfg["diaspora_share"]),
            "ground_truth": NONE,
        }
    )
