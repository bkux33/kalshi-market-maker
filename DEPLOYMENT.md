# Deployment

Nothing here requires paid infrastructure. A laptop is enough for research; a small VPS
(1 vCPU / 1–2 GB RAM, ~$5–6/month) is enough for 24/7 recording + paper trading. Budget disk:
a busy order-book feed can produce several GB of tape per week.

## 1. Local development
```bash
python3.11 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev,llm]"
cp .env.example .env            # fill in KALSHI_API_KEY_ID / KALSHI_PRIVATE_KEY_PATH (demo first)
pytest -q                       # full test suite, no network needed

# pipeline check without any credentials:
alphalab synthetic --markets 20 --seed 1
alphalab experiment --strategy imbalance --grid '{"threshold":[0.6,0.7]}'
alphalab dashboard              # http://127.0.0.1:8080
```

## 2. Collect data and research
```bash
alphalab discover --series KXBTC15M KXETH15M          # public REST, no key needed
alphalab record --series KXBTC15M KXETH15M --external # needs API key (Kalshi WS requires auth)
alphalab ingest                                       # load closed hourly tape files into DuckDB
alphalab features                                     # predictive-feature study
alphalab research-loop                                # all strategy families + report in reports/
alphalab scorecard                                    # scorecards of every experiment
alphalab ask "Is performance concentrated in one market?"
```
Optional: import the third-party samples used during development (not redistributed):
`alphalab import-csv baseline <path>/example_data.csv`, `alphalab import-csv crypto_sample <path>/sample_features.csv`.

## 3. Paper trading
```bash
# on live data (simulated fills, same fill model as the backtest):
alphalab paper --strategy market_maker --params '{"min_edge_c": 1.0}' --series KXBTC15M --record
# or driven by recorded data (clock = event time):
alphalab paper --replay --strategy imbalance --params '{"threshold": 0.7}'
alphalab ingest                                   # load finished journals
alphalab paper-evaluate '<strategy_key from `alphalab scorecard`/dashboard>'
```
Paper sessions write `data/journal/<run>.jsonl` and `data/state/paper_state.json` (dashboard).

## 4. Docker (recording + paper + dashboard)
```bash
cp .env.example .env && mkdir -p secrets && cp /path/to/kalshi.pem secrets/kalshi.pem && chmod 600 secrets/kalshi.pem
docker compose up -d recorder ingester paper dashboard
docker compose logs -f paper
# dashboard binds to 127.0.0.1:8080 only; from your laptop:
ssh -L 8080:127.0.0.1:8080 user@your-vps
```
The compose file never enables live trading. The private key is mounted as a Docker secret.

## 5. Production (live) — only after RISK.md's checklist
Run the same image with `TRADING_MODE=live`, `LIVE_TRADING_ACK=I_ACCEPT_REAL_MONEY_RISK`,
`KALSHI_ENV=prod`, conservative `risk:` limits, and
`alphalab live --strategy <name> --params '<exact validated params>' --series ...`.
Run it under a supervisor (systemd or `restart: unless-stopped`), keep the kill-switch file on a
persistent volume, and monitor the dashboard/`risk_events`. Do the `--demo-integration` dry run on
the demo exchange first.

## Operations
* Emergency stop: `alphalab kill --reason "..."` (or the dashboard button / a physical button).
* Resume: fix the cause, `alphalab kill --status`, then `alphalab kill --reset`, then restart.
* Backups: `data/raw/` (source of truth) and `data/alphalab.duckdb` (rebuildable from tape).
* Integrity: `alphalab db-check`.
