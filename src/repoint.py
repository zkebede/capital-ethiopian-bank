"""S4 re-point reference: the same behavioural features on a US bank's outbound remittances.

Usage::

    python -m src.repoint

The demo re-points the app from the Ethiopian bank's inbound payouts to a
fictional US bank's outbound transfers to East Africa. Only two of the six
features exist on this data, so they are computed per party and reported as
flags, not a risk score:

- **sender** ``near_threshold_ct``: transfers in [0.90, 1.00) × threshold
  sent by one sender in any 30-day window (structuring, mirrored).
- **beneficiary** ``sender_fanin``: distinct senders paying one beneficiary in
  any 30-day window (collector, mirrored).

Thresholds come from ``config.yaml → features``, unchanged from S2. Features use
the upload file (no labels); ``data/raw/eval/ground_truth.csv`` is read only to
check them. ``beneficiary_country`` is never used.

Writes ``data/derived/us_party_flags.csv`` and ``data/derived/_s4_expected.md``.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

import pandas as pd

from src.common import DEFAULT_CONFIG, DERIVED_DIR, NONE, RAW_DIR, load_config
from src.features import OPS

# party type → (id column, feature, typology it mirrors)
PARTIES: dict[str, tuple[str, str, str]] = {
    "sender": ("sender_id", "near_threshold_ct", "structuring"),
    "beneficiary": ("beneficiary_id", "sender_fanin", "collector"),
}


def _window_pairs(us: pd.DataFrame, key: str, cols: list[str], days: int) -> pd.DataFrame:
    """Pair each transfer with every transfer of the same party in the ``days`` up to and including it."""
    right = us[[key, "send_date", *cols]].rename(columns={c: f"{c}_prev" for c in ["send_date", *cols]})
    pairs = us[[key, "txn_id", "send_date"]].merge(right, on=key)
    start = pairs["send_date"] - pd.Timedelta(days=days)
    return pairs[(pairs["send_date_prev"] > start) & (pairs["send_date_prev"] <= pairs["send_date"])]


def sender_near_threshold(cfg: dict[str, Any], us: pd.DataFrame) -> pd.Series:
    """Per sender: most near-threshold transfers in any 30-day window."""
    lo, hi = (b * float(cfg["reporting_threshold_usd"]) for b in cfg["features"]["near_threshold_band"])
    near = us[(us["amount_usd"] >= lo) & (us["amount_usd"] < hi)]
    pairs = _window_pairs(near, "sender_id", [], int(cfg["features"]["window_days"]))
    worst = pairs.groupby(["sender_id", "txn_id"]).size().groupby(level="sender_id").max()
    return worst.reindex(us["sender_id"].unique(), fill_value=0).rename("value")


def beneficiary_fanin(cfg: dict[str, Any], us: pd.DataFrame) -> pd.Series:
    """Per beneficiary: most distinct senders in any 30-day window."""
    pairs = _window_pairs(us, "beneficiary_id", ["sender_id"], int(cfg["features"]["window_days"]))
    worst = pairs.groupby(["beneficiary_id", "txn_id"])["sender_id_prev"].nunique().groupby(level=0).max()
    return worst.reindex(us["beneficiary_id"].unique(), fill_value=0).rename("value")


def party_flags(cfg: dict[str, Any], us: pd.DataFrame) -> pd.DataFrame:
    """One row per sender and per beneficiary: feature value and whether it flags."""
    rules = cfg["features"]["rules"]
    values = {"sender": sender_near_threshold(cfg, us), "beneficiary": beneficiary_fanin(cfg, us)}
    frames = []
    for party, (_, feature, typology) in PARTIES.items():
        r = rules[feature]
        v = values[party]
        frames.append(
            pd.DataFrame(
                {
                    "party_type": party,
                    "party_id": v.index,
                    "feature": feature,
                    "value": v.to_numpy(),
                    "threshold": r["threshold"],
                    "flagged": OPS[r["op"]](v, float(r["threshold"])).to_numpy(),
                    "suggested_typology": typology,
                }
            )
        )
    return pd.concat(frames, ignore_index=True).sort_values(["party_type", "party_id"], ignore_index=True)


def party_truth(us: pd.DataFrame, truth: pd.DataFrame) -> pd.Series:
    """Hidden label per (party_type, party_id): the mirrored typology if any of its transfers carry it."""
    labelled = us.merge(
        truth.loc[truth["file"].eq("us_outbound"), ["key", "ground_truth"]], left_on="txn_id", right_on="key"
    )
    out = {}
    for party, (col, _, typology) in PARTIES.items():
        hit = labelled.loc[labelled["ground_truth"].eq(typology), col].unique()
        out.update({(party, pid): typology for pid in hit})
    return pd.Series(out, dtype=object)


def detection_table(flags: pd.DataFrame, labels: pd.Series) -> pd.DataFrame:
    """Planted vs ordinary parties, and how many of each are flagged."""
    key = pd.MultiIndex.from_frame(flags[["party_type", "party_id"]])
    planted = pd.Series(labels.reindex(key).fillna(NONE).ne(NONE).to_numpy(), index=flags.index)
    rows = []
    for party, (_, feature, typology) in PARTIES.items():
        sel = flags["party_type"].eq(party)
        p, n = sel & planted, sel & ~planted
        rows.append(
            {
                "party_type": party,
                "feature": feature,
                "typology": typology,
                "planted": int(p.sum()),
                "planted_flagged": int(flags.loc[p, "flagged"].sum()),
                "ordinary": int(n.sum()),
                "ordinary_flagged": int(flags.loc[n, "flagged"].sum()),
            }
        )
    return pd.DataFrame(rows).set_index("party_type")


def expected_md(cfg: dict[str, Any], us: pd.DataFrame, flags: pd.DataFrame, det: pd.DataFrame) -> str:
    """Numbers the Foundry re-point should match."""
    rules = cfg["features"]["rules"]
    lines = [
        f"# S4 re-point expected results — {cfg['us_outbound']['bank_name']}",
        "",
        f"`us_outbound`: {len(us):,} transfers · {us['sender_id'].nunique():,} senders · "
        f"{us['beneficiary_id'].nunique():,} beneficiaries. **Synthetic data.**",
        "",
        "Same thresholds as S2, applied to the worst 30-day window per party:",
        "",
        "| Party | Feature | Flag |",
        "|---|---|---|",
    ]
    lines += [
        f"| {party} | `{feature}` | {rules[feature]['op']} {rules[feature]['threshold']} |"
        for party, (_, feature, _) in PARTIES.items()
    ]
    lines += [
        "",
        "## Flagged parties (match these in Foundry)",
        "",
        "| Party | Flagged | Of |",
        "|---|---:|---:|",
    ]
    for party in PARTIES:
        sel = flags["party_type"].eq(party)
        lines.append(f"| {party} | {int(flags.loc[sel, 'flagged'].sum())} | {int(sel.sum()):,} |")
    lines += [
        "",
        "## Check against hidden labels (evaluation only)",
        "",
        "| Party | Mirrors | Planted flagged | Ordinary flagged |",
        "|---|---|---:|---:|",
    ]
    lines += [
        f"| {party} | {r.typology} | {r.planted_flagged} / {r.planted} | {r.ordinary_flagged} / {r.ordinary:,} |"
        for party, r in det.iterrows()
    ]
    lines += [
        "",
        "Only two of the six S2 features exist on outbound data, so parties are flagged, not scored.",
        "Flagged parties go to the same agent and analyst Actions.",
    ]
    return "\n".join(lines) + "\n"


def load_us(raw: Path) -> pd.DataFrame:
    """Read the label-free upload copy of ``us_outbound``."""
    path = raw / "upload" / "us_outbound.csv"
    if not path.exists():
        raise SystemExit(f"{path} not found — run `python -m src.generate` first")
    return pd.read_csv(path, parse_dates=["send_date"])


def main(argv: list[str] | None = None) -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--raw", default=str(RAW_DIR))
    parser.add_argument("--derived", default=str(DERIVED_DIR))
    args = parser.parse_args(argv)

    cfg = load_config(args.config)
    raw, derived = Path(args.raw), Path(args.derived)
    us = load_us(raw)
    flags = party_flags(cfg, us)
    derived.mkdir(parents=True, exist_ok=True)
    flags.to_csv(derived / "us_party_flags.csv", index=False, lineterminator="\n")

    truth = pd.read_csv(raw / "eval" / "ground_truth.csv")  # checking only
    det = detection_table(flags, party_truth(us, truth))
    report = expected_md(cfg, us, flags, det)
    (derived / "_s4_expected.md").write_text(report, encoding="utf-8")
    print(report)
    print(f"Written to {derived / '_s4_expected.md'}")


if __name__ == "__main__":
    main()
