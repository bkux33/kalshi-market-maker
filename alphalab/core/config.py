"""Configuration: YAML file + environment variables.

Precedence (highest first): environment variables, YAML file, defaults below.
Secrets (API key id, private key path) come ONLY from the environment.

``TRADING_MODE`` defaults to ``paper``. ``live`` is refused unless the separate
acknowledgement variable is set exactly (see ``alphalab.execution.live``).
"""

from __future__ import annotations

import dataclasses
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

PROD_REST = "https://api.elections.kalshi.com/trade-api/v2"
DEMO_REST = "https://demo-api.kalshi.co/trade-api/v2"
PROD_WS = "wss://api.elections.kalshi.com/trade-api/ws/v2"
DEMO_WS = "wss://demo-api.kalshi.co/trade-api/ws/v2"

LIVE_ACK_VALUE = "I_ACCEPT_REAL_MONEY_RISK"


@dataclass
class KalshiConfig:
    env: str = "demo"                     # demo | prod
    api_key_id: Optional[str] = None      # env only: KALSHI_API_KEY_ID
    private_key_path: Optional[str] = None  # env only: KALSHI_PRIVATE_KEY_PATH
    rest_url: Optional[str] = None
    ws_url: Optional[str] = None
    requests_per_second: float = 8.0      # client-side throttle, below Kalshi basic tier
    timeout_s: float = 10.0

    @property
    def rest_base(self) -> str:
        return self.rest_url or (PROD_REST if self.env == "prod" else DEMO_REST)

    @property
    def ws_base(self) -> str:
        return self.ws_url or (PROD_WS if self.env == "prod" else DEMO_WS)

    @property
    def has_credentials(self) -> bool:
        return bool(self.api_key_id and self.private_key_path)


@dataclass
class DataConfig:
    dir: str = "data"
    db_file: str = "alphalab.duckdb"
    raw_subdir: str = "raw"
    journal_subdir: str = "journal"
    state_subdir: str = "state"
    tob_min_interval_ms: int = 0          # 0 = record every top-of-book change

    @property
    def root(self) -> Path:
        return Path(self.dir)

    @property
    def db_path(self) -> Path:
        return self.root / self.db_file

    @property
    def raw_dir(self) -> Path:
        return self.root / self.raw_subdir

    @property
    def journal_dir(self) -> Path:
        return self.root / self.journal_subdir

    @property
    def state_dir(self) -> Path:
        return self.root / self.state_subdir


@dataclass
class FeeConfig:
    taker_rate: float = 0.07
    maker_rate: float = 0.0175            # conservative default; see core/fees.py
    rounding: str = "ceil_cent"
    series: Dict[str, Dict[str, Any]] = field(default_factory=dict)  # series -> {fee_type, fee_multiplier}


@dataclass
class FillConfig:
    """Explicit, configurable fill assumptions for simulation (backtest AND paper)."""
    order_latency_ms: int = 150           # decision -> order live at exchange
    cancel_latency_ms: int = 150          # cancel request -> effective (fills can happen meanwhile)
    maker_fill_mode: str = "queue"        # queue | cross_only
    queue_cancel_model: str = "pro_rata"  # pro_rata | back (conservative) | front (optimistic)
    allow_touch_fill: bool = False        # cross_only mode: fill when opposite best == our price
    trade_through_fills_all: bool = True  # a print through our price fills our remaining size
    taker_slippage_ticks: int = 0         # extra adverse ticks applied to every taker fill
    unknown_depth_qty: float = 0.0        # size assumed at levels whose size is unknown (0 = none)
    seed: int = 7


@dataclass
class RiskConfig:
    max_order_size: float = 10
    max_position_per_market: float = 50
    max_market_exposure_usd: float = 50.0
    max_total_exposure_usd: float = 200.0
    max_strategy_exposure_usd: float = 150.0
    max_daily_loss_usd: float = 25.0
    max_drawdown_usd: float = 50.0
    max_orders_per_second: float = 5.0
    max_open_orders: int = 20
    stale_data_s: float = 5.0
    min_price_units: int = 100            # 1c
    max_price_units: int = 9900           # 99c
    max_distance_from_mid_units: int = 1500   # 15c price-sanity band
    settlement_buffer_s: float = 60.0     # no new orders this close to close_time
    duplicate_window_s: float = 2.0
    on_disconnect: str = "cancel_all"     # cancel_all | hold
    kill_switch_file: str = "data/KILL_SWITCH"


def research_risk_config(base: "RiskConfig | None" = None) -> "RiskConfig":
    """Risk limits for research backtests: the same position/exposure/sanity
    limits as paper/live, but no P&L-based halts, so the full loss
    distribution of a strategy is observed instead of being truncated."""
    import dataclasses as _dc
    b = base or RiskConfig()
    return _dc.replace(b, max_daily_loss_usd=float("inf"), max_drawdown_usd=float("inf"))


@dataclass
class ValidationConfig:
    train_frac: float = 0.6
    validation_frac: float = 0.2          # remainder is the untouched test set
    walk_forward_folds: int = 4
    max_grid_size: int = 64
    min_trades_research: int = 30
    min_trades_paper: int = 300
    min_distinct_days_paper: int = 5
    min_distinct_markets_paper: int = 10
    max_single_market_pnl_share: float = 0.5
    max_single_day_pnl_share: float = 0.5
    max_is_oos_degradation: float = 0.5   # OOS expectancy must keep >= 50% of IS
    min_wf_positive_frac: float = 0.6
    stress_fee_multiplier: float = 2.0
    stress_slippage_ticks: int = 1
    stress_latency_ms: int = 250
    max_pvalue_vs_random: float = 0.05
    random_benchmark_trials: int = 200
    suspicious_sharpe: float = 5.0        # annualisation-free per-trade Sharpe * sqrt(n) guard
    max_param_count: int = 6
    min_paper_fills_live: int = 200
    min_paper_days_live: int = 10
    max_paper_backtest_gap: float = 0.5   # paper expectancy must be >= 50% of backtest OOS


@dataclass
class LLMConfig:
    enabled: bool = False
    model: str = "claude-opus-5"
    max_tokens: int = 4096


@dataclass
class Settings:
    trading_mode: str = "paper"           # paper | live
    live_ack: Optional[str] = None        # env only: LIVE_TRADING_ACK
    kalshi: KalshiConfig = field(default_factory=KalshiConfig)
    data: DataConfig = field(default_factory=DataConfig)
    fees: FeeConfig = field(default_factory=FeeConfig)
    fill: FillConfig = field(default_factory=FillConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    validation: ValidationConfig = field(default_factory=ValidationConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    series: List[str] = field(default_factory=lambda: ["KXBTC15M", "KXETH15M", "KXSOL15M", "KXXRP15M"])
    markets: List[str] = field(default_factory=list)

    def redacted(self) -> Dict[str, Any]:
        d = dataclasses.asdict(self)
        d["kalshi"]["api_key_id"] = _mask(self.kalshi.api_key_id)
        d["kalshi"]["private_key_path"] = "<set>" if self.kalshi.private_key_path else None
        d["live_ack"] = "<set>" if self.live_ack else None
        return d


def _mask(v: Optional[str]) -> Optional[str]:
    if not v:
        return None
    return v[:4] + "…" if len(v) > 8 else "…"


def _merge(obj: Any, data: Dict[str, Any]) -> Any:
    for k, v in (data or {}).items():
        if not hasattr(obj, k):
            raise ValueError(f"Unknown config key: {k}")
        cur = getattr(obj, k)
        if dataclasses.is_dataclass(cur) and isinstance(v, dict):
            _merge(cur, v)
        else:
            setattr(obj, k, v)
    return obj


_SECRET_KEYS = {("kalshi", "api_key_id"), ("kalshi", "private_key_path"), ("live_ack",)}


def load_settings(path: Optional[str] = None, env: Optional[Dict[str, str]] = None) -> Settings:
    env = dict(os.environ if env is None else env)
    s = Settings()
    cfg_path = path or env.get("ALPHALAB_CONFIG")
    if cfg_path and Path(cfg_path).exists():
        raw = yaml.safe_load(Path(cfg_path).read_text()) or {}
        if "live_ack" in raw or any(k in (raw.get("kalshi") or {}) for k in ("api_key_id", "private_key_path")):
            raise ValueError("Secrets must be provided via environment variables, not the YAML config")
        _merge(s, raw)
    if env.get("TRADING_MODE"):
        s.trading_mode = env["TRADING_MODE"].strip().lower()
    if s.trading_mode not in ("paper", "live"):
        raise ValueError(f"TRADING_MODE must be 'paper' or 'live', got {s.trading_mode!r}")
    s.live_ack = env.get("LIVE_TRADING_ACK")
    if env.get("KALSHI_ENV"):
        s.kalshi.env = env["KALSHI_ENV"].strip().lower()
    if s.kalshi.env not in ("demo", "prod"):
        raise ValueError("KALSHI_ENV must be 'demo' or 'prod'")
    s.kalshi.api_key_id = env.get("KALSHI_API_KEY_ID") or None
    s.kalshi.private_key_path = env.get("KALSHI_PRIVATE_KEY_PATH") or None
    if env.get("DATA_DIR"):
        s.data.dir = env["DATA_DIR"]
        s.risk.kill_switch_file = str(Path(env["DATA_DIR"]) / "KILL_SWITCH")
    if env.get("KILL_SWITCH_FILE"):
        s.risk.kill_switch_file = env["KILL_SWITCH_FILE"]
    if env.get("ANTHROPIC_API_KEY") and env.get("ALPHALAB_LLM", "").lower() in ("1", "true", "yes"):
        s.llm.enabled = True
    return s
