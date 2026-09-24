"""Automated research loop.

1. ingest newly recorded tape files
2. build the feature dataset (same FeatureEngine as trading)
3. predictive-feature study (discovery vs confirmation) -> hypotheses
4. crypto: incremental-information test of spot features beyond the price
5. logical-arbitrage scan
6. bounded experiments for every strategy family with applicable data
7. classification (REJECT / RESEARCH / PAPER); PAPER candidates are listed
   for the paper trader - nothing is promoted to live automatically
8. markdown research report

Every step is deterministic given the database contents.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from alphalab.core.config import Settings
from alphalab.data.db import Database
from alphalab.data.ingest import ingest_tape
from alphalab.research import arb_scan, predictive
from alphalab.research.experiments import ExperimentSpec, run_experiment
from alphalab.research.reports import md_table, research_report

log = logging.getLogger(__name__)

# Bounded default grids (each well under ValidationConfig.max_grid_size).
DEFAULT_GRIDS: Dict[str, Dict[str, Any]] = {
    "imbalance": {"grid": {"threshold": [0.55, 0.6, 0.65, 0.7], "horizon_s": [1, 3, 5, 10, 30]},
                  "base": {"max_spread_c": 3.0}},
    "market_maker": {"grid": {"min_edge_c": [0.25, 1.0, 2.0], "gamma": [0.05, 0.2]}, "base": {}},
    "momentum": {"grid": {"lookback_s": [5, 10, 30], "min_move_c": [1.0, 2.0], "horizon_s": [10, 30]}, "base": {}},
    "mean_reversion": {"grid": {"lookback_s": [5, 10, 30], "min_move_c": [1.0, 2.0], "horizon_s": [10, 30]},
                       "base": {}},
    "vol_breakout": {"grid": {"ratio": [0.6, 0.8], "horizon_s": [10, 30]}, "base": {}},
    "vol_fade": {"grid": {"ratio": [0.6, 0.8], "horizon_s": [10, 30]}, "base": {}},
    "crypto15m_fair_value": {"grid": {"edge_c": [3.0, 5.0, 8.0], "min_tts_s": [60.0, 300.0]}, "base": {}},
    "logical_arb": {"grid": {"min_edge_c": [0.0, 1.0]}, "base": {}},
}


def _source_groups(db: Database) -> Dict[str, List[str]]:
    df = db.query_df("""SELECT m.ticker, COALESCE(m.source, 'unknown') AS src FROM markets m
                        WHERE EXISTS (SELECT 1 FROM tob t WHERE t.market = m.ticker) ORDER BY 2, 1""")
    out: Dict[str, List[str]] = {}
    for r in df.itertuples(index=False):
        grp = r.src.split(":")[0] + (":" + r.src.split(":")[1] if r.src.startswith("import:") else "")
        grp = "recorded" if r.src.startswith("tape:") else grp
        out.setdefault(grp, []).append(r.ticker)
    return out


def _applicable(db: Database, strategy: str, markets: List[str]) -> Optional[str]:
    """Return None if the strategy can be tested on these markets, else a reason."""
    if not markets:
        return "no market data"
    ph = ",".join("?" for _ in markets)
    if strategy == "crypto15m_fair_value":
        n = int(db.query_df(f"""SELECT COUNT(*) n FROM markets m WHERE ticker IN ({ph}) AND floor_strike IS NOT NULL
                                AND underlying IS NOT NULL
                                AND EXISTS (SELECT 1 FROM external_prices e WHERE e.symbol = m.underlying)""",
                            markets)["n"].iloc[0])
        return None if n > 0 else "no markets with strike + external reference price"
    if strategy == "logical_arb":
        n = int(db.query_df(f"""SELECT COUNT(*) n FROM (SELECT event_ticker FROM markets WHERE ticker IN ({ph})
                                AND event_ticker IS NOT NULL GROUP BY 1 HAVING COUNT(*) >= 2
                                AND (BOOL_OR(mutually_exclusive) OR COUNT(floor_strike) >= 2))""",
                            markets)["n"].iloc[0])
        return None if n > 0 else "no events with >= 2 related markets"
    return None


def run_research_loop(db: Database, settings: Settings, strategies: Optional[List[str]] = None,
                      out_dir: str = "reports", random_trials: Optional[int] = None,
                      progress: Optional[Callable[[str], None]] = None) -> Dict[str, Any]:
    say = progress or (lambda s: log.info(s))
    summary: Dict[str, Any] = {}
    if settings.data.raw_dir.exists():
        summary["ingest"] = ingest_tape(db, settings.data.raw_dir)
        say(f"ingest: {summary['ingest']}")
    # Research is run separately per data source: sources differ in depth quality and period,
    # and mixing them would let one source's quirks masquerade as a signal in another.
    groups = _source_groups(db)
    summary["data_groups"] = {g: len(ms) for g, ms in groups.items()}
    sections: List[str] = []
    exps: Dict[str, Any] = {}
    summary["feature_study"] = {}
    summary["crypto_incremental_info"] = {}
    for group, markets in groups.items():
        say(f"[{group}] building feature dataset for {len(markets)} markets ...")
        ds = predictive.build_dataset(db, markets=markets)
        if ds.empty:
            continue
        fs = predictive.feature_study(db, ds, notes=f"source={group}")
        surv = fs[fs["survives"]] if not fs.empty else fs
        summary["feature_study"][group] = {
            "rows": int(len(ds)), "pairs_tested": int(len(fs) // 2) if not fs.empty else 0,
            "surviving_pairs": sorted({f"{r.feature}@{r.horizon_s:g}s" for r in surv.itertuples()}),
            "split": fs["notes"].iloc[0] if not fs.empty else ""}
        say(f"[{group}] feature study: {summary['feature_study'][group]}")
        if not fs.empty:
            conf = fs[(fs.split == "confirmation") & (fs.n > 0)].copy()
            conf = conf.reindex(conf["ic_tstat"].abs().sort_values(ascending=False).index).head(15)
            cols = ["feature", "horizon_s", "n", "ic", "ic_tstat", "top_bucket_move", "bottom_bucket_move",
                    "cost_hurdle", "net_edge_top", "net_edge_bottom"]
            sections.append(f"## Feature study - {group} (confirmation set, 15 strongest |t|)\n\n"
                            + md_table(conf[cols].round(4)))
        if {"strike_z", "fwd_settle"}.issubset(ds.columns) and ds["strike_z"].notna().any():
            summary["crypto_incremental_info"][group] = {
                "strike_z": predictive.incremental_information(ds, ["strike_z"]),
                "strike_z+spot_ret_60s": predictive.incremental_information(ds, ["strike_z", "spot_ret_60s"])}
        for name in strategies or list(DEFAULT_GRIDS):
            key = f"{name}@{group}"
            why = _applicable(db, name, markets)
            if why:
                exps[key] = {"skipped": why}
                say(f"skip {key}: {why}")
                continue
            g = DEFAULT_GRIDS[name]
            base = dict(g["base"])
            if name == "crypto15m_fair_value":
                base.setdefault("series_prefix", "KX")
            spec = ExperimentSpec(name=f"loop:{key}", strategy=name, base_params=base, grid=g["grid"],
                                  markets=markets, fill=settings.fill, fees=settings.fees,
                                  validation=settings.validation, random_trials=random_trials,
                                  notes=f"source={group}")
            try:
                rep = run_experiment(db, spec, progress=say)
                exps[key] = {"experiment_id": rep.experiment_id, "classification": rep.classification,
                             "reason": rep.reason, "oos_net": rep.oos_metrics.get("net_pnl"),
                             "oos_mid_pnl": rep.oos_metrics.get("mid_pnl"),
                             "oos_trades": rep.oos_metrics.get("n_trades"),
                             "gating_flags": [f["code"] for f in rep.flags if f["gating"]]}
            except ValueError as exc:
                exps[key] = {"skipped": str(exc)}
                say(f"skip {key}: {exc}")
    if summary["crypto_incremental_info"]:
        sections.append("## Crypto incremental-information tests\n\n```\n" +
                        json.dumps(summary["crypto_incremental_info"], indent=2, default=str) + "\n```")
    summary["arb_scan"] = arb_scan.scan(db, taker_rate=settings.fees.taker_rate)
    sections.append("## Logical-arbitrage scan\n\n```\n" + json.dumps(summary["arb_scan"], indent=2, default=str)
                    + "\n```")
    summary["experiments"] = exps
    summary["paper_candidates"] = [k for k, v in exps.items() if v.get("classification") == "PAPER"]
    sections.insert(0, "## Research-loop summary\n\n```\n" + json.dumps(summary, indent=2, default=str) + "\n```")
    path = research_report(db, out_dir, title="Kalshi Alpha Lab - research loop report", extra_sections=sections)
    summary["report"] = str(path)
    return summary
