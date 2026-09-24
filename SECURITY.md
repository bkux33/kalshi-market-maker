# Security

## Secrets
* API key id, private-key path, live-trading acknowledgement, dashboard token and Anthropic key are
  read **only from environment variables**. The YAML loader rejects configs containing them.
* Never commit: `.env`, `*.pem`, `*.key`, key `.txt` files, `data/` (tape, journals, DuckDB, state),
  `reports/`. All are in `.gitignore`; CI fails if private-key material is found in tracked files.
* Private keys: keep outside the repository, `chmod 600`, mount as a Docker secret in containers.
* The private key object lives in `KalshiSigner` (name-mangled attribute), never appears in `repr`,
  and parse errors do not echo file contents (tested).
* Logs: every log line passes through a redaction filter that removes PEM blocks, bearer/Anthropic
  tokens and fields named like signatures, keys, secrets or passwords (tested).
* `alphalab config` and the dashboard `/api/settings` show redacted settings only.

## Trading safety
* Default mode is paper; live requires six independent gates (RISK.md), including earned
  LIVE-CANDIDATE status and hard caps that can only be raised by changing code.
* The kill switch is a latched file; dashboards can trip it but only the CLI can reset it.
* The LLM assistant is read-only and cannot reach execution code (tested).

## Network exposure
* The dashboard binds to `127.0.0.1` by default and in Docker Compose. If exposed, set
  `DASHBOARD_TOKEN` (bearer token checked on every `/api` call) and put it behind TLS/SSH.
* The dashboard does not render untrusted HTML: all values are escaped client-side.

## Audit performed (2026-09-24)
| Item | Result |
|---|---|
| Tracked files scanned for private keys / tokens (`git grep`) | none (only test fixtures that generate throwaway keys at runtime and redaction regexes) |
| `.gitignore` covers keys, `.env`, data, journals, DuckDB | yes |
| Secrets rejected from YAML config | yes (test) |
| Private key never logged / repr'd | yes (tests) |
| SQL built from user input | parameterised queries; table/column names are constants |
| Dashboard auth | optional bearer token; localhost binding by default; kill-switch reset not exposed |
| Live order path reachable from LLM | no (test asserts the assistant module does not import execution/kalshi code and exposes no order tools) |
| Dependencies | mainstream packages only (duckdb, pandas, numpy, httpx, websockets, cryptography, fastapi, uvicorn, pyyaml, anthropic optional) |

Known gaps: no rate limiting or CSRF protection on the dashboard beyond the token (keep it on
localhost); DuckDB files are not encrypted at rest; journal files contain order history (treat
`data/` as sensitive).

## Reporting
Open a private security advisory on the repository rather than a public issue.
