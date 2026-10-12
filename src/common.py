"""Shared helpers: config loading, seeded RNG streams, the date window, and name pools."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = ROOT / "config.yaml"
RAW_DIR = ROOT / "data" / "raw"
EXPORTS_DIR = ROOT / "data" / "exports"

NONE = "none"
LAUNDERING_TYPOLOGIES = (
    "structuring",
    "collector",
    "pass_through",
    "mule_ring",
    "fx_out_of_profile",
)

# Stable stream ids so editing one generator never shifts another's random draws.
STREAMS = {
    "customers": 1,
    "fx_rates": 2,
    "typologies": 3,
    "remittances": 4,
    "wallets": 5,
    "fx": 6,
    "alerts": 7,
    "us_outbound": 8,
}

# Culture-appropriate Ethiopian given names. Ethiopian names are patronymic
# (given name + father's given name), so one pool serves both positions.
# Names are drawn independently of every risk pattern.
ETHIOPIAN_NAMES = [
    "Abebe",
    "Abel",
    "Abraham",
    "Addis",
    "Alemayehu",
    "Almaz",
    "Amanuel",
    "Aster",
    "Ayana",
    "Bekele",
    "Belay",
    "Bereket",
    "Betelhem",
    "Bizunesh",
    "Chaltu",
    "Dagim",
    "Dawit",
    "Derartu",
    "Desta",
    "Eleni",
    "Elias",
    "Eyerusalem",
    "Fasil",
    "Fatuma",
    "Feven",
    "Fikru",
    "Gebre",
    "Genet",
    "Girma",
    "Hana",
    "Haile",
    "Hirut",
    "Kalkidan",
    "Kebede",
    "Kidist",
    "Lemlem",
    "Lensa",
    "Liya",
    "Mahlet",
    "Mekdes",
    "Meron",
    "Mesfin",
    "Meseret",
    "Mohammed",
    "Mulugeta",
    "Nardos",
    "Negash",
    "Rahel",
    "Rediet",
    "Ruth",
    "Samuel",
    "Selam",
    "Semira",
    "Seble",
    "Solomon",
    "Tadesse",
    "Tamrat",
    "Tesfaye",
    "Tigist",
    "Tsion",
    "Wondimu",
    "Worku",
    "Yared",
    "Yohannes",
    "Yonas",
    "Zelalem",
    "Zewdu",
    "Zinash",
    "Abdi",
    "Ahmed",
    "Hawi",
    "Gemechu",
    "Lelise",
    "Obse",
    "Tolosa",
    "Kedir",
    "Nuria",
    "Biniam",
    "Saba",
    "Tewodros",
]

# Mixed East African given names / family names for the US-outbound beneficiaries.
# Drawn independently of beneficiary country and of every risk pattern.
EAST_AFRICAN_GIVEN = [
    "Abebe",
    "Almaz",
    "Dawit",
    "Hana",
    "Wanjiru",
    "Otieno",
    "Achieng",
    "Kamau",
    "Amina",
    "Hodan",
    "Faisal",
    "Ayaan",
    "Nakato",
    "Mugisha",
    "Okello",
    "Neema",
    "Baraka",
    "Rehema",
    "Semhar",
    "Yonas",
    "Akech",
    "Deng",
    "Idil",
    "Zawadi",
]
EAST_AFRICAN_FAMILY = [
    "Tesfaye",
    "Bekele",
    "Mwangi",
    "Odhiambo",
    "Kariuki",
    "Warsame",
    "Abdi",
    "Nsubuga",
    "Mutebi",
    "Mollel",
    "Massawe",
    "Tekle",
    "Garang",
    "Mohamed",
    "Haile",
    "Njoroge",
    "Kiprop",
    "Wekesa",
]


@dataclass(frozen=True)
class Window:
    """The simulated activity window [start, end) derived from config."""

    start: pd.Timestamp
    end: pd.Timestamp

    @property
    def days(self) -> int:
        """Number of whole days in the window."""
        return int((self.end - self.start).days)


def load_config(path: str | Path = DEFAULT_CONFIG) -> dict[str, Any]:
    """Load the YAML config into a plain dict."""
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def window(cfg: dict[str, Any]) -> Window:
    """Return the activity window: start_date plus `months` calendar months."""
    start = pd.Timestamp(cfg["start_date"])
    return Window(start=start, end=start + pd.DateOffset(months=int(cfg["months"])))


def rng_for(cfg: dict[str, Any], stream: str) -> np.random.Generator:
    """Return an independent, reproducible RNG for a named stream."""
    return np.random.default_rng([int(cfg["seed"]), STREAMS[stream]])


def uniform_int(rng: np.random.Generator, bounds: list[int], size: int | None = None):
    """Draw integers uniformly from the inclusive range ``[lo, hi]``."""
    lo, hi = bounds
    return rng.integers(lo, hi + 1, size=size)


def random_timestamps(
    rng: np.random.Generator, win: Window, size: int, day_weights: np.ndarray | None = None
) -> pd.DatetimeIndex:
    """Draw timestamps inside the window, optionally weighting whole days."""
    p = None if day_weights is None else day_weights / day_weights.sum()
    day = rng.choice(win.days, size=size, p=p)
    seconds = rng.integers(7 * 3600, 21 * 3600, size=size)  # business-ish hours
    return win.start + pd.to_timedelta(day, unit="D") + pd.to_timedelta(seconds, unit="s")


def holiday_day_weights(cfg: dict[str, Any], win: Window) -> np.ndarray:
    """Per-day volume weights with a lift in the lead-up to each holiday."""
    days = pd.date_range(win.start, periods=win.days, freq="D")
    weights = np.ones(win.days)
    lead = pd.Timedelta(days=int(cfg["holiday_lead_days"]))
    for h in pd.to_datetime(cfg["holidays"]):
        weights[(days >= h - lead) & (days <= h)] = float(cfg["holiday_volume_multiplier"])
    return weights


def ethiopian_names(rng: np.random.Generator, size: int) -> np.ndarray:
    """Draw patronymic Ethiopian full names (given name + father's name)."""
    pool = np.array(ETHIOPIAN_NAMES)
    given = rng.choice(pool, size=size)
    father = rng.choice(pool, size=size)
    return np.char.add(np.char.add(given.astype(str), " "), father.astype(str))


def distinct_ethiopian_names(rng: np.random.Generator, size: int) -> np.ndarray:
    """Draw ``size`` full names with no repeats (for "unrelated" sender sets)."""
    pool = np.array(ETHIOPIAN_NAMES)
    combos = len(pool) ** 2
    if size > combos:
        raise ValueError(f"Cannot draw {size} distinct names from {combos} combinations")
    idx = rng.choice(combos, size=size, replace=False)
    given, father = pool[idx // len(pool)], pool[idx % len(pool)]
    return np.char.add(np.char.add(given.astype(str), " "), father.astype(str))


def sender_countries(cfg: dict[str, Any], rng: np.random.Generator, size: int) -> np.ndarray:
    """Draw sender countries from the configured weights, expanding ``other``."""
    weights = cfg["sender_countries"]
    keys = list(weights)
    p = np.array([weights[k] for k in keys], dtype=float)
    picked = rng.choice(keys, size=size, p=p / p.sum()).astype(object)
    other = picked == "other"
    picked[other] = rng.choice(cfg["other_sender_countries"], size=int(other.sum()))
    return picked.astype(str)


def daily_rate_lookup(rates: pd.Series, ts: pd.Series | pd.DatetimeIndex) -> np.ndarray:
    """Official ETB/USD rate for the calendar day of each timestamp."""
    return rates.reindex(pd.DatetimeIndex(pd.to_datetime(ts)).normalize()).to_numpy()


def fmt_ts(ts: pd.Series | pd.DatetimeIndex) -> pd.Series:
    """Format timestamps as ISO strings so CSV output is byte-stable."""
    return pd.Series(pd.to_datetime(ts)).dt.strftime("%Y-%m-%d %H:%M:%S")


def zero_pad_ids(prefix: str, numbers: np.ndarray, width: int) -> np.ndarray:
    """Build ids like ``C000123`` from integer arrays."""
    numbers = np.asarray(numbers)
    if numbers.size == 0:
        return np.array([], dtype=str)
    return np.char.add(prefix, np.char.zfill(numbers.astype(str), width))
