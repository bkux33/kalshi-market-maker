"""Does a feature predict future price moves - and by enough to pay for trading?

``build_dataset`` replays recorded data through the same ``FeatureEngine``
that strategies use and samples features on a regular clock per market, then
attaches forward mid changes (cents) at several horizons plus the move to
settlement.

``feature_study`` evaluates each (feature, horizon) pair separately on a
chronological DISCOVERY set and a later CONFIRMATION set (split by market):

* Spearman information coefficient (IC) and its t-statistic, computed on
  *non-overlapping* samples (one sample per horizon per market) so that
  overlapping forward windows do not inflate significance;
* hit rate of sign(feature) vs sign(move);
* mean forward move in the top and bottom feature quintiles;
* the round-trip taker cost hurdle (observed spread + 2 x taker fee), and
  the net edge of trading the extreme quintiles after that hurdle.

A feature is marked ``survives`` only if the IC has the same sign and |t| > 2
in both sets AND the confirmation-set extreme-quintile move beats the cost
hurdle. Most features are expected not to survive.
"""

from __future__ import annotations

import math
import time
import uuid
from typing import Any, Dict, Iterable, List, Optional, Sequence

import numpy as np
import pandas as pd

from alphalab.core.book_manager import BookManager
from alphalab.core.fees import fee_per_contract
from alphalab.data.db import Database
from alphalab.research.features import FeatureEngine
from alphalab.sim.replay import ReplaySource, ReplaySpec

NS = 1_000_000_000
DEFAULT_FEATURES = ("imbalance", "imbalance_1", "micro_minus_mid", "ret_1s", "ret_5s", "ret_10s", "ret_30s",
                    "ret_60s", "vol_10s", "vol_60s", "trade_imbalance_10s", "trade_imbalance_60s",
                    "trade_intensity_60s", "accel", "spread", "strike_z", "spot_ret_10s", "spot_ret_60s",
                    "tts_s")
DEFAULT_HORIZONS = (1, 3, 5, 10, 30, 60)


def build_dataset(db: Database, markets: Optional[List[str]] = None, sample_every_s: float = 1.0,
                  horizons: Sequence[float] = DEFAULT_HORIZONS) -> pd.DataFrame:
    src = ReplaySource(db, ReplaySpec(markets=markets))
    books = BookManager()
    fe = FeatureEngine(market_meta=src.meta)
    mids: Dict[str, tuple] = {}
    rows: List[Dict[str, Any]] = []
    next_sample: Dict[str, int] = {}
    step = int(sample_every_s * NS)
    settle: Dict[str, float] = {}
    for ev in src:
        k = ev.kind
        if k in ("snapshot", "delta"):
            books.apply(ev)
            synced = books.is_synced(ev.market)
            ob = books.book(ev.market)
            fe.on_book(ev.market, ob, ev.ts_ns, synced)
            if not synced or ob.mid() is None:
                continue
            ts_l, m_l = mids.setdefault(ev.market, ([], []))
            ts_l.append(ev.ts_ns)
            m_l.append(ob.mid() / 100.0)
            if ev.ts_ns >= next_sample.get(ev.market, 0):
                rows.append(fe.features(ev.market, ev.ts_ns))
                next_sample[ev.market] = ev.ts_ns + step
        elif k == "trade":
            fe.on_trade(ev)
        elif k == "external":
            fe.on_external(ev)
        elif k == "settlement":
            settle[ev.market] = ev.value * 100.0
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    out = []
    for m, g in df.groupby("market", sort=True):
        ts_l, m_l = mids[m]
        ts_a, m_a = np.asarray(ts_l), np.asarray(m_l)
        g = g.sort_values("ts_ns").copy()
        for h in horizons:
            idx = np.searchsorted(ts_a, g["ts_ns"].to_numpy() + int(h * NS), side="right") - 1
            fwd = m_a[np.clip(idx, 0, len(m_a) - 1)]
            valid = (g["ts_ns"].to_numpy() + int(h * NS)) <= ts_a[-1]
            g[f"fwd_{h:g}s"] = np.where(valid, fwd - g["mid"].to_numpy(), np.nan)
        g["fwd_settle"] = (settle[m] - g["mid"]) if m in settle else np.nan
        out.append(g)
    res = pd.concat(out, ignore_index=True)
    close = {m: v.get("close_ts") for m, v in src.meta.items()}
    res["order_ts"] = res["market"].map(lambda m: close.get(m) or 0)
    return res


def _spearman(x: np.ndarray, y: np.ndarray) -> float:
    if len(x) < 3:
        return float("nan")
    rx = pd.Series(x).rank().to_numpy()
    ry = pd.Series(y).rank().to_numpy()
    if rx.std() == 0 or ry.std() == 0:
        return float("nan")
    return float(np.corrcoef(rx, ry)[0, 1])


def _non_overlapping(g: pd.DataFrame, horizon_s: float) -> pd.DataFrame:
    keep, last = [], -math.inf
    for i, ts in enumerate(g["ts_ns"].to_numpy()):
        if ts - last >= horizon_s * NS:
            keep.append(i)
            last = ts
    return g.iloc[keep]


def evaluate(df: pd.DataFrame, feature: str, target: str, horizon_s: float, taker_rate: float = 0.07) -> Dict[str, Any]:
    d = df[[feature, target, "spread", "mid", "ts_ns", "market"]].dropna()
    if d.empty:
        return {"n": 0}
    d = pd.concat([_non_overlapping(g, horizon_s) for _, g in d.groupby("market", sort=True)])
    n = len(d)
    if n < 20 or d[feature].nunique() < 3:
        return {"n": n}
    x, y = d[feature].to_numpy(float), d[target].to_numpy(float)
    ic = _spearman(x, y)
    t = ic * math.sqrt((n - 2) / max(1e-12, 1 - ic * ic)) if not math.isnan(ic) else float("nan")
    nz = (x != 0) & (y != 0)
    hit = float(np.mean(np.sign(x[nz]) == np.sign(y[nz]))) if nz.any() else float("nan")
    q = pd.qcut(pd.Series(x).rank(method="first"), 5, labels=False)
    top, bot = y[q.to_numpy() == 4], y[q.to_numpy() == 0]
    fees_c = np.array([fee_per_contract(int(round(m * 100)), taker_rate) * 100 for m in d["mid"]])
    hurdle = float(np.mean(d["spread"].to_numpy()) + 2 * np.mean(fees_c))
    top_m, bot_m = float(np.mean(top)), float(np.mean(bot))
    # trade direction implied by the IC sign: long top / short bottom if IC > 0
    sign = 1.0 if (not math.isnan(ic) and ic >= 0) else -1.0
    return {"n": n, "ic": ic, "ic_tstat": t, "hit_rate": hit, "top_bucket_move": top_m,
            "bottom_bucket_move": bot_m, "cost_hurdle": hurdle,
            "net_edge_top": sign * top_m - hurdle, "net_edge_bottom": -sign * bot_m - hurdle}


def feature_study(db: Database, df: Optional[pd.DataFrame] = None, features: Iterable[str] = DEFAULT_FEATURES,
                  horizons: Sequence[float] = DEFAULT_HORIZONS, discovery_frac: float = 0.6,
                  persist: bool = True, notes: str = "") -> pd.DataFrame:
    if df is None:
        df = build_dataset(db, horizons=horizons)
    if df.empty:
        return pd.DataFrame()
    markets = df.drop_duplicates("market").sort_values(["order_ts", "market"])["market"].tolist()
    n_disc = max(1, int(round(len(markets) * discovery_frac)))
    if len(markets) >= 4:
        disc_m, conf_m = set(markets[:n_disc]), set(markets[n_disc:])
        disc, conf = df[df["market"].isin(disc_m)], df[df["market"].isin(conf_m)]
        split_note = f"market split {len(disc_m)}/{len(conf_m)}"
    else:  # too few markets: split each market's timeline instead
        cut = df.groupby("market")["ts_ns"].transform(lambda s: s.quantile(discovery_frac))
        disc, conf = df[df["ts_ns"] <= cut], df[df["ts_ns"] > cut]
        split_note = "time split within markets (few markets)"
    study_id = f"study-{uuid.uuid4().hex[:8]}"
    rows = []
    for f in features:
        if f not in df.columns:
            continue
        for h in horizons:
            tgt = f"fwd_{h:g}s"
            if tgt not in df.columns:
                continue
            for split, part in (("discovery", disc), ("confirmation", conf)):
                r = evaluate(part, f, tgt, h)
                rows.append({"study_id": study_id, "feature": f, "horizon_s": float(h), "split": split, **r})
    res = pd.DataFrame(rows)
    if res.empty:
        return res
    piv = res.pivot_table(index=["feature", "horizon_s"], columns="split", values=["ic", "ic_tstat"], aggfunc="first")
    surv = {}
    for (f, h), r in piv.iterrows():
        try:
            icd, icc = r[("ic", "discovery")], r[("ic", "confirmation")]
            td, tc = r[("ic_tstat", "discovery")], r[("ic_tstat", "confirmation")]
        except KeyError:
            continue
        conf_row = res[(res.feature == f) & (res.horizon_s == h) & (res.split == "confirmation")].iloc[0]
        edge = max(conf_row.get("net_edge_top", -1) or -1, conf_row.get("net_edge_bottom", -1) or -1)
        surv[(f, h)] = bool(np.sign(icd) == np.sign(icc) and abs(td) > 2 and abs(tc) > 2 and edge > 0)
    res["survives"] = [surv.get((f, h), False) for f, h in zip(res.feature, res.horizon_s)]
    res["notes"] = f"{split_note}; {notes}".strip("; ")
    if persist:
        keep = ["study_id", "feature", "horizon_s", "split", "n", "ic", "ic_tstat", "hit_rate", "top_bucket_move",
                "bottom_bucket_move", "cost_hurdle", "net_edge_top", "net_edge_bottom", "notes"]
        p = res[[c for c in keep if c in res.columns]].copy()
        p["created_ns"] = time.time_ns()
        p["data_hash"] = str(len(df))
        db.insert_df("predictive_results", p)
    return res


# ---------------------------------------------------------------------- incremental information
def _logit(p: np.ndarray) -> np.ndarray:
    p = np.clip(p, 1e-4, 1 - 1e-4)
    return np.log(p / (1 - p))


def fit_logistic(X: np.ndarray, y: np.ndarray, l2: float = 1.0, iters: int = 50) -> np.ndarray:
    X1 = np.column_stack([np.ones(len(X)), X])
    w = np.zeros(X1.shape[1])
    reg = np.eye(len(w)) * l2
    reg[0, 0] = 0.0
    for _ in range(iters):
        z = np.clip(X1 @ w, -30, 30)
        p = 1 / (1 + np.exp(-z))
        g = X1.T @ (p - y) + reg @ w
        H = X1.T @ (X1 * (p * (1 - p))[:, None]) + reg
        step = np.linalg.solve(H, g)
        w -= step
        if np.max(np.abs(step)) < 1e-8:
            break
    return w


def _predict(w: np.ndarray, X: np.ndarray) -> np.ndarray:
    z = np.clip(np.column_stack([np.ones(len(X)), X]) @ w, -30, 30)
    return 1 / (1 + np.exp(-z))


def incremental_information(df: pd.DataFrame, extra_features: Sequence[str], discovery_frac: float = 0.6,
                            sample_every_s: float = 30.0) -> Dict[str, Any]:
    """Does adding ``extra_features`` to the market's own price improve
    out-of-sample prediction of settlement?  Baseline model: logit(mid).
    Samples are thinned to one per ``sample_every_s`` per market, and the
    comparison is made market by market (markets are the independent units)."""
    need = ["mid", "fwd_settle", "market", "ts_ns", "order_ts", *extra_features]
    d = df[[c for c in need if c in df.columns]].dropna().copy()
    if d.empty or not all(f in d.columns for f in extra_features):
        return {"ok": False, "reason": "missing data"}
    d["y"] = ((d["fwd_settle"] + d["mid"]) >= 50).astype(float)
    d = pd.concat([_non_overlapping(g, sample_every_s) for _, g in d.groupby("market", sort=True)])
    markets = d.drop_duplicates("market").sort_values(["order_ts", "market"])["market"].tolist()
    if len(markets) < 6:
        return {"ok": False, "reason": f"only {len(markets)} settled markets; need >= 6", "n_markets": len(markets)}
    cut = int(round(len(markets) * discovery_frac))
    tr, te = d[d.market.isin(markets[:cut])], d[d.market.isin(markets[cut:])]
    xb = lambda part: _logit(part["mid"].to_numpy() / 100.0)[:, None]
    xf = lambda part: np.column_stack([xb(part), part[list(extra_features)].to_numpy(float)])
    mu, sd = xf(tr).mean(axis=0), xf(tr).std(axis=0) + 1e-9
    wb = fit_logistic(xb(tr), tr["y"].to_numpy())
    wf = fit_logistic((xf(tr) - mu) / sd, tr["y"].to_numpy())
    pb = _predict(wb, xb(te))
    pf = _predict(wf, (xf(te) - mu) / sd)
    y = te["y"].to_numpy()
    ll = lambda p: -(y * np.log(np.clip(p, 1e-6, 1)) + (1 - y) * np.log(np.clip(1 - p, 1e-6, 1)))
    te = te.assign(llb=ll(pb), llf=ll(pf), bb=(pb - y) ** 2, bf=(pf - y) ** 2, p_mkt=te["mid"] / 100.0)
    per_m = te.groupby("market")[["llb", "llf", "bb", "bf"]].mean()
    diff = per_m["llb"] - per_m["llf"]  # > 0 means the extra features helped
    n = len(diff)
    t = float(diff.mean() / (diff.std(ddof=1) / math.sqrt(n))) if n > 1 and diff.std(ddof=1) > 0 else float("nan")
    return {"ok": True, "n_train_markets": cut, "n_test_markets": n, "n_test_samples": int(len(te)),
            "baseline_logloss": float(te["llb"].mean()), "model_logloss": float(te["llf"].mean()),
            "baseline_brier": float(te["bb"].mean()), "model_brier": float(te["bf"].mean()),
            "mean_improvement_per_market": float(diff.mean()), "t_stat_markets": t,
            "features": list(extra_features),
            "verdict": ("adds information (t>2 across markets)" if (not math.isnan(t) and t > 2)
                        else "no demonstrated incremental information")}
