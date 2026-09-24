# Data sources and provenance

| Source | How | Licence / terms | Stored as |
|---|---|---|---|
| Kalshi WebSocket (`orderbook_delta`, `trade`, `ticker`, `market_lifecycle_v2`) | `alphalab record` | Kalshi API terms of service (your own account) | raw frames in `data/raw/` tape |
| Kalshi REST (markets, series fee metadata, results) | recorder + discovery | same | `market`/`series` tape records |
| Coinbase public ticker (optional) | `alphalab record --external` | Coinbase API terms | `ext` tape records |
| `example_data.csv` from rnop/Kalshi-Prediction-Market-Trading-Bot-Public-Baseline | `alphalab import-csv baseline` | repository has **no licence**; used locally only, **not redistributed** | snapshots, Binance prices, settlements flagged `inferred` |
| `sample_features.csv` from kapelame/kalshi-crypto-bot | `alphalab import-csv crypto_sample` | MIT per README; not redistributed | top-of-book with **assumed** depth, settlements inferred from pinned final quotes |
| Synthetic generator | `alphalab synthetic` | this project | flagged `depth_quality = synthetic` |

Rules:
* Recorded market data never goes into git (`data/` is ignored). Recorded data may be subject to
  Kalshi's terms; do not publish it.
* Every settlement row records whether it is official (`inferred = false`, from Kalshi) or inferred.
* Every market row records `depth_quality` (`full`, `top_only_assumed_<n>`, `synthetic`) and `source`;
  the experiment engine turns weak data quality into gating flags.
