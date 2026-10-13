"""Planted laundering typologies — one function per pattern.

Typologies are *behaviours*. Planted customers are drawn at random from the whole
population (only ``fx_out_of_profile`` filters, by occupation income band, which
is the behaviour being planted). Names, region, branch, and sender country are
never consulted when choosing who gets a pattern.

Every planting function returns plain DataFrames whose ``ground_truth`` column
carries the typology name. Ids and FX rates are assigned later by the
per-file generators, so these functions stay small and testable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from src.common import (
    ETHIOPIAN_NAMES,
    daily_rate_lookup,
    distinct_ethiopian_names,
    ethiopian_names,
    rng_for,
    sender_countries,
    uniform_int,
    window,
)
from src.gen_customers import BRANCHES, occupation_bands
from src.gen_fx import rate_move_days

REMIT_COLS = ["receiver_id", "sender_name", "sender_country", "mto", "amount_usd", "payout_date", "ground_truth"]
WALLET_COLS = ["customer_id", "counterparty_wallet", "amount_etb", "timestamp", "channel", "ground_truth"]
FX_COLS = ["customer_id", "usd_in", "timestamp", "ground_truth"]


def wallet_of(customer_ids: np.ndarray | pd.Series) -> np.ndarray:
    """Each customer holds exactly one wallet: ``C000123`` → ``W000123``."""
    return np.char.replace(np.asarray(customer_ids).astype(str), "C", "W", count=1)


def month_starts(cfg: dict[str, Any]) -> list[pd.Timestamp]:
    """First day of each month in the window."""
    win = window(cfg)
    return [win.start + pd.DateOffset(months=i) for i in range(int(cfg["months"]))]


def _random_time_in_day(rng: np.random.Generator, day: pd.Timestamp) -> pd.Timestamp:
    return day + pd.Timedelta(seconds=int(rng.integers(8 * 3600, 18 * 3600)))


# ---------------------------------------------------------------------------
# Remittance-side typologies
# ---------------------------------------------------------------------------


def plant_structuring(cfg: dict[str, Any], rng: np.random.Generator, customer_ids: np.ndarray) -> pd.DataFrame:
    """3+ payouts per month at 90–99% of the reporting threshold, a few days apart."""
    p = cfg["typology_params"]["structuring"]
    threshold = float(cfg["reporting_threshold_usd"])
    months = month_starts(cfg)
    mtos = cfg["mtos"]
    rows = []
    for cid in customer_ids:
        senders = ethiopian_names(rng, int(rng.integers(1, 3)))
        countries = sender_countries(cfg, rng, len(senders))
        for m in rng.choice(len(months), size=int(uniform_int(rng, p["active_months"])), replace=False):
            k = int(uniform_int(rng, p["payouts_per_month"]))
            gaps = rng.integers(2, 6, size=k - 1)  # "a few days apart"
            first = int(rng.integers(0, 28 - gaps.sum()))
            offsets = first + np.concatenate([[0], np.cumsum(gaps)])
            amounts = np.floor(threshold * rng.uniform(*p["band"], size=k) / 10) * 10
            for off, amt in zip(offsets, amounts):
                s = int(rng.integers(len(senders)))
                rows.append(
                    (
                        cid,
                        senders[s],
                        countries[s],
                        rng.choice(mtos),
                        float(amt),
                        _random_time_in_day(rng, months[m] + pd.Timedelta(days=int(off))),
                        "structuring",
                    )
                )
    return pd.DataFrame(rows, columns=REMIT_COLS)


def plant_collector(cfg: dict[str, Any], rng: np.random.Generator, customer_ids: np.ndarray) -> pd.DataFrame:
    """One receiver with 40+ unrelated senders inside a single month."""
    p = cfg["typology_params"]["collector"]
    months = month_starts(cfg)
    frames = []
    for cid in customer_ids:
        for m in rng.choice(len(months), size=int(uniform_int(rng, p["active_months"])), replace=False):
            n = int(uniform_int(rng, p["senders_per_month"]))
            days = rng.integers(0, 28, size=n)
            secs = rng.integers(8 * 3600, 20 * 3600, size=n)
            frames.append(
                pd.DataFrame(
                    {
                        "receiver_id": cid,
                        "sender_name": distinct_ethiopian_names(rng, n),
                        "sender_country": sender_countries(cfg, rng, n),
                        "mto": rng.choice(cfg["mtos"], size=n),
                        "amount_usd": np.round(rng.uniform(*p["amount_usd"], size=n) / 5) * 5,
                        "payout_date": months[m] + pd.to_timedelta(days, unit="D") + pd.to_timedelta(secs, unit="s"),
                        "ground_truth": "collector",
                    }
                )
            )
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=REMIT_COLS)


def plant_pass_through(
    cfg: dict[str, Any], rng: np.random.Generator, customer_ids: np.ndarray, rates: pd.Series
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Each inflow is followed by 80%+ of its value leaving via wallet within 6 hours.

    Returns ``(remittances, wallet_txns)``.
    """
    p = cfg["typology_params"]["pass_through"]
    win = window(cfg)
    max_h = float(p["max_hours"])
    remits, wallets = [], []
    for cid in customer_ids:
        n = int(uniform_int(rng, p["inflows"]))
        senders = ethiopian_names(rng, int(rng.integers(1, 5)))
        countries = sender_countries(cfg, rng, len(senders))
        days = rng.choice(win.days - 1, size=n, replace=False)
        for d in days:
            ts = _random_time_in_day(rng, win.start + pd.Timedelta(days=int(d)))
            usd = float(np.round(rng.uniform(*p["amount_usd"]) / 5) * 5)
            s = int(rng.integers(len(senders)))
            remits.append((cid, senders[s], countries[s], rng.choice(cfg["mtos"]), usd, ts, "pass_through"))

            etb_moved = usd * daily_rate_lookup(rates, [ts])[0] * rng.uniform(*p["moved_share"])
            k = int(rng.integers(1, 4))
            shares = rng.dirichlet(np.ones(k))
            hours = np.minimum(np.cumsum(rng.uniform(0.2, 1.5, size=k)), max_h - 0.05)
            for share, h in zip(shares, hours):
                wallets.append(
                    (
                        cid,
                        f"EXT-{int(rng.integers(100000, 999999))}",
                        round(float(etb_moved * share), 2),
                        ts + pd.Timedelta(hours=float(h)),
                        "p2p",
                        "pass_through",
                    )
                )
    return pd.DataFrame(remits, columns=REMIT_COLS), pd.DataFrame(wallets, columns=WALLET_COLS)


# ---------------------------------------------------------------------------
# Customer-side typologies
# ---------------------------------------------------------------------------


def plant_mule_rings(
    cfg: dict[str, Any], rng: np.random.Generator, customers: pd.DataFrame, ring_members: list[np.ndarray]
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """5–8 customers sharing a phone or address, cycling funds among themselves.

    Mutates shared identifiers on ``customers`` (a copy is returned) and returns
    ``(customers, wallet_txns)``.
    """
    p = cfg["typology_params"]["mule_ring"]
    win = window(cfg)
    customers = customers.copy()
    idx = customers.set_index("customer_id").index
    frames = []
    for i, members in enumerate(ring_members):
        rows = idx.get_indexer(members)
        shared = "phone" if i % 2 == 0 else "address"
        customers.iloc[rows, customers.columns.get_loc(shared)] = customers.iloc[rows[0]][shared]
        customers.iloc[rows, customers.columns.get_loc("ground_truth")] = "mule_ring"

        n = int(uniform_int(rng, p["txns_per_ring"]))
        size = len(members)
        step = np.arange(n)
        order = rng.permutation(members)
        ts = win.start + pd.to_timedelta(np.sort(rng.uniform(0, win.days - 1, size=n)), unit="D")
        frames.append(
            pd.DataFrame(
                {
                    "customer_id": order[step % size],
                    "counterparty_wallet": wallet_of(order[(step + 1) % size]),
                    "amount_etb": np.round(rng.uniform(*p["amount_etb"], size=n), -2),
                    "timestamp": ts.round("s"),
                    "channel": "p2p",
                    "ground_truth": "mule_ring",
                }
            )
        )
    wallets = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=WALLET_COLS)
    return customers, wallets


def plant_fx_out_of_profile(
    cfg: dict[str, Any], rng: np.random.Generator, customer_ids: np.ndarray, rates: pd.Series
) -> pd.DataFrame:
    """Large conversions within 24h of a >2% rate move, by low-income occupations."""
    fx = cfg["fx"]
    move_days = rate_move_days(cfg, rates)
    window_h = float(fx["window_hours"])
    rows = []
    for cid in customer_ids:
        for day in rng.choice(move_days, size=int(uniform_int(rng, fx["planted_per_customer"]))):
            ts = pd.Timestamp(day) + pd.Timedelta(hours=float(rng.uniform(0.5, window_h - 1)))
            rows.append((cid, round(float(rng.uniform(*fx["planted_usd"])), 2), ts.round("s"), "fx_out_of_profile"))
    return pd.DataFrame(rows, columns=FX_COLS)


def plant_duplicate_profiles(
    cfg: dict[str, Any], rng: np.random.Generator, customers: pd.DataFrame, pairs: np.ndarray
) -> pd.DataFrame:
    """Same person at two branches with a small name variation.

    This is a *data-quality* issue for entity resolution, not laundering. The
    duplicate row copies the original's dob and phone, moves to a different
    branch, and gets a variant of the name (e.g. "Abebe Kebede" → "Abebe K.").
    ``pairs`` is an ``(n, 2)`` array of ``(original_id, duplicate_id)``.
    """
    customers = customers.copy()
    pos = customers.set_index("customer_id").index
    orig = pos.get_indexer(pairs[:, 0])
    dup = pos.get_indexer(pairs[:, 1])

    # Variants: abbreviated father's name, given name only, or grandfather's name added.
    names = customers["full_name"].to_numpy()[orig]
    variant = rng.integers(0, 3, size=len(pairs))
    grandfather = rng.choice(ETHIOPIAN_NAMES, size=len(pairs))
    new_names = []
    for name, v, gf in zip(names, variant, grandfather):
        given, father = name.split(" ")[:2]
        new_names.append((f"{given} {father[0]}.", given, f"{name} {gf}")[v])

    branch_names = np.array(list(BRANCHES))
    orig_branch = customers["branch"].to_numpy()[orig]
    new_branch = np.array([rng.choice(branch_names[branch_names != b]) for b in orig_branch])

    for col in ("dob", "phone", "occupation"):
        customers.iloc[dup, customers.columns.get_loc(col)] = customers[col].to_numpy()[orig]
    customers.iloc[dup, customers.columns.get_loc("full_name")] = new_names
    customers.iloc[dup, customers.columns.get_loc("branch")] = new_branch
    customers.iloc[dup, customers.columns.get_loc("region")] = [BRANCHES[b][0] for b in new_branch]
    customers.iloc[np.concatenate([orig, dup]), customers.columns.get_loc("ground_truth")] = "duplicate_profile"
    return customers


# ---------------------------------------------------------------------------
# Legitimate look-alikes (ground_truth = none)
#
# Ordinary behaviour that trips the same features as a typology. They give the
# agent genuine borderline calls; the distinguishing facts are behavioural
# (one-off vs recurring, reported vs avoided, money cycling or not).
# ---------------------------------------------------------------------------

EVENT_COLS = ["customer_id", "timestamp", "scenario"]


def _events(remits: pd.DataFrame, scenario: str) -> pd.DataFrame:
    return pd.DataFrame(
        {"customer_id": remits["receiver_id"], "timestamp": remits["payout_date"], "scenario": scenario}
    )


def plant_community_collection(
    cfg: dict[str, Any], rng: np.random.Generator, customer_ids: np.ndarray
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """A one-off collection (funeral, wedding): many small gifts from different senders in one month.

    Looks like ``collector`` on fan-in, but happens once, with small amounts.
    Returns ``(remittances, events)``.
    """
    p = cfg["lookalikes"]["community_collection"]
    months = month_starts(cfg)
    frames = []
    for cid in customer_ids:
        m = int(rng.integers(len(months)))
        n = int(uniform_int(rng, p["senders"]))
        start = int(rng.integers(0, 20))  # gifts arrive over about a week
        days = start + rng.integers(0, 8, size=n)
        frames.append(
            pd.DataFrame(
                {
                    "receiver_id": cid,
                    "sender_name": distinct_ethiopian_names(rng, n),
                    "sender_country": sender_countries(cfg, rng, n),
                    "mto": rng.choice(cfg["mtos"], size=n),
                    "amount_usd": np.round(rng.uniform(*p["amount_usd"], size=n) / 5) * 5,
                    "payout_date": months[m]
                    + pd.to_timedelta(days, unit="D")
                    + pd.to_timedelta(rng.integers(8 * 3600, 20 * 3600, size=n), unit="s"),
                    "ground_truth": "none",
                }
            )
        )
    remits = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=REMIT_COLS)
    return remits, _events(remits, "community_collection")


def plant_steady_trader(
    cfg: dict[str, Any], rng: np.random.Generator, customer_ids: np.ndarray
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """A business owner paid by the same partners every month, often just under the threshold.

    Looks like ``structuring`` on near-threshold counts, but runs all year and many
    payouts are at or above the threshold (so reported). Returns ``(remittances, events)``.
    """
    p = cfg["lookalikes"]["steady_trader"]
    thr = float(cfg["reporting_threshold_usd"])
    band = cfg["typology_params"]["structuring"]["band"]
    months = month_starts(cfg)
    rows = []
    for cid in customer_ids:
        partners = ethiopian_names(rng, int(rng.integers(1, 3)))
        countries = sender_countries(cfg, rng, len(partners))
        for month in months:
            k = int(uniform_int(rng, p["payouts_per_month"]))
            for day in np.sort(rng.choice(28, size=k, replace=False)):
                near = rng.random() < float(p["near_share"])
                amt = thr * rng.uniform(*band) if near else rng.uniform(*p["above_usd"])
                s = int(rng.integers(len(partners)))
                rows.append(
                    (
                        cid,
                        partners[s],
                        countries[s],
                        rng.choice(cfg["mtos"]),
                        float(np.floor(amt / 10) * 10),
                        _random_time_in_day(rng, month + pd.Timedelta(days=int(day))),
                        "none",
                    )
                )
    remits = pd.DataFrame(rows, columns=REMIT_COLS)
    return remits, _events(remits, "steady_trader")


def plant_family_shared_phone(
    cfg: dict[str, Any], rng: np.random.Generator, customers: pd.DataFrame, groups: list[np.ndarray]
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Relatives registered on one phone. No money moves between them.

    Looks like ``mule_ring`` on shared identifiers, without the cycling.
    Returns ``(customers, events)``; each member gets one alert anchor.
    """
    win = window(cfg)
    customers = customers.copy()
    pos = customers.set_index("customer_id").index
    phone = customers.columns.get_loc("phone")
    frames = []
    for members in groups:
        rows = pos.get_indexer(members)
        customers.iloc[rows, phone] = customers.iloc[rows[0], phone]
        ts = win.start + pd.to_timedelta(rng.uniform(30, win.days - 1, size=len(members)), unit="D")
        frames.append(
            pd.DataFrame({"customer_id": members, "timestamp": ts.round("s"), "scenario": "family_shared_phone"})
        )
    events = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=EVENT_COLS)
    return customers, events


def plant_lookalikes(
    cfg: dict[str, Any], customers: pd.DataFrame, taken: set[str]
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Pick customers not used by any typology and plant every look-alike.

    Uses its own RNG stream so the typologies above keep their exact draws.
    Returns ``(customers with scenario set, remittances, events)``.
    """
    rng = rng_for(cfg, "lookalikes")
    lk = cfg["lookalikes"]
    ids = customers["customer_id"].to_numpy()
    available = ~np.isin(ids, list(taken))

    def take(n: int, mask: np.ndarray | None = None) -> np.ndarray:
        pool = np.flatnonzero(available & (mask if mask is not None else True))
        if n > len(pool):
            raise ValueError(f"Not enough customers for {n} look-alikes (pool {len(pool)})")
        picked = rng.choice(pool, size=n, replace=False)
        available[picked] = False
        return ids[picked]

    trader_occ = customers["occupation"].eq(lk["steady_trader"]["occupation"]).to_numpy()
    community = take(int(lk["community_collection"]["customers"]))
    traders = take(int(lk["steady_trader"]["customers"]), trader_occ)
    sizes = uniform_int(rng, lk["family_shared_phone"]["size"], size=int(lk["family_shared_phone"]["groups"]))
    families = [take(int(s)) for s in sizes]

    customers = customers.copy()
    scenario = customers.columns.get_loc("scenario")
    pos = customers.set_index("customer_id").index
    for name, picked in (
        ("community_collection", community),
        ("steady_trader", traders),
        ("family_shared_phone", np.concatenate(families) if families else np.array([], dtype=str)),
    ):
        customers.iloc[pos.get_indexer(picked), scenario] = name
    customers.iloc[
        pos.get_indexer(np.concatenate([community, traders])), customers.columns.get_loc("diaspora_flag")
    ] = True

    cc_remit, cc_events = plant_community_collection(cfg, rng, community)
    tr_remit, tr_events = plant_steady_trader(cfg, rng, traders)
    customers, fam_events = plant_family_shared_phone(cfg, rng, customers, families)
    remits = pd.concat([cc_remit, tr_remit], ignore_index=True)
    events = pd.concat([cc_events, tr_events, fam_events], ignore_index=True)
    return customers, remits, events


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


@dataclass
class Planted:
    """Everything the planting step produces for the per-file generators."""

    customers: pd.DataFrame
    remittances: pd.DataFrame
    wallet_txns: pd.DataFrame
    fx_conversions: pd.DataFrame
    ring_members: list[np.ndarray] = field(default_factory=list)
    lookalike_events: pd.DataFrame = field(default_factory=lambda: pd.DataFrame(columns=EVENT_COLS))


def select_planted_customers(cfg: dict[str, Any], rng: np.random.Generator, customers: pd.DataFrame) -> dict:
    """Pick disjoint customer sets for each typology, uniformly at random.

    ``fx_out_of_profile`` is drawn from low-income occupations because the
    behaviour *is* converting amounts far outside the occupation's profile.
    """
    planted = cfg["planted"]
    ring_size = cfg["typology_params"]["mule_ring"]["size"]
    bands = occupation_bands(cfg)
    ids = customers["customer_id"].to_numpy()
    available = np.ones(len(ids), dtype=bool)

    def take(n: int, mask: np.ndarray | None = None) -> np.ndarray:
        pool = np.flatnonzero(available & (mask if mask is not None else True))
        if n > len(pool):
            raise ValueError(f"Not enough customers to plant {n} (pool {len(pool)})")
        picked = rng.choice(pool, size=n, replace=False)
        available[picked] = False
        return ids[picked]

    low_income = customers["occupation"].map(bands).eq("low").to_numpy()
    sizes = uniform_int(rng, ring_size, size=int(planted["mule_rings"]))
    return {
        "fx_out_of_profile": take(int(planted["fx_out_of_profile"]), low_income),
        "structuring": take(int(planted["structuring"])),
        "collector": take(int(planted["collector"])),
        "pass_through": take(int(planted["pass_through"])),
        "mule_ring": [take(int(s)) for s in sizes],
        "duplicate_profile": take(2 * int(planted["duplicate_profile_pairs"])).reshape(-1, 2),
    }


def plant_all(cfg: dict[str, Any], customers: pd.DataFrame, rates: pd.Series) -> Planted:
    """Choose planted customers and generate every typology's rows."""
    rng = rng_for(cfg, "typologies")
    picks = select_planted_customers(cfg, rng, customers)

    customers = customers.copy()
    gt_col = customers.columns.get_loc("ground_truth")
    pos = customers.set_index("customer_id").index
    for typ in ("structuring", "collector", "pass_through", "fx_out_of_profile"):
        rows = pos.get_indexer(picks[typ])
        customers.iloc[rows, gt_col] = typ
        if typ != "fx_out_of_profile":  # these customers receive diaspora inflows
            customers.iloc[rows, customers.columns.get_loc("diaspora_flag")] = True

    structuring = plant_structuring(cfg, rng, picks["structuring"])
    collector = plant_collector(cfg, rng, picks["collector"])
    pt_remit, pt_wallet = plant_pass_through(cfg, rng, picks["pass_through"], rates)
    customers, ring_wallet = plant_mule_rings(cfg, rng, customers, picks["mule_ring"])
    fx = plant_fx_out_of_profile(cfg, rng, picks["fx_out_of_profile"], rates)
    customers = plant_duplicate_profiles(cfg, rng, customers, picks["duplicate_profile"])

    taken = set(customers.loc[customers["ground_truth"].ne("none"), "customer_id"])
    customers, lk_remit, lk_events = plant_lookalikes(cfg, customers, taken)

    return Planted(
        customers=customers,
        remittances=pd.concat([structuring, collector, pt_remit, lk_remit], ignore_index=True),
        wallet_txns=pd.concat([pt_wallet, ring_wallet], ignore_index=True),
        fx_conversions=fx,
        ring_members=picks["mule_ring"],
        lookalike_events=lk_events,
    )
