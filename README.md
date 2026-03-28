# Polymarket CLOB Trading Bot MVP

## Overview
Automated Python 3.11 bot for Polymarket CLOB with PAPER and LIVE modes, SQLite journaling, Telegram reports, risk engine, compounding sizing, Market Making + Mispricing/Event strategies, and UTC daily controls.

## Project Structure
- `app/` bootstrap and runtime entrypoints (`python -m app.main`)
- `core/` models, config, state, logging, time utils
- `utils/` math and retry helpers
- `exchange/` exchange interface + paper/live clients + market selection
- `strategies/` market-making and mispricing strategies
- `risk/` risk and dynamic sizing engine
- `persistence/` sqlite schema and repository
- `reporting/` telegram notifier and reports
- `services/` scheduler, execution, pnl, positions
- `config/` markets and risk yaml configs
- `docker/` Dockerfile

## Environment Variables
Copy `.env.example` to `.env` and set values.

## Run with Docker (recommended)
```bash
docker compose up -d --build
```
Container command is `python -m app.main`.

## Run locally
> Use module mode (do **not** assume `python app/main.py`).

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
MODE=PAPER TELEGRAM_ENABLED=false DB_PATH=./data/bot.sqlite3 python -m app.main
```

## PAPER Mode workflow (first)
1. Start in PAPER mode.
2. Verify startup log/notifier shows configured markets and selected outcomes.
3. Check fills, PnL snapshots, and daily metrics in SQLite.
4. Tune `config/risk.yaml` sizing and stop settings before any LIVE test.

## LIVE Mode (limited / requires validation)
```bash
MODE=LIVE docker compose up -d --build
```
LIVE credentials are fail-fast validated at startup. The live client currently uses assumed endpoint mappings (`/markets`, `/book`, `/orders`, `/fills`, `/positions`) and must be validated in staging/sandbox before real capital.

## Data and Logs
- SQLite DB: `./data/bot.sqlite3` (or `DB_PATH` if overridden)
- Logs: `./logs/bot.log`

## Strategy Behavior (current)
- **Outcome selection:** one configured outcome per market, probability band filtered and liquidity-biased.
- **Market-making:** periodic quote refresh, inventory skew/reduce-only controls.
- **Mispricing lifecycle:** signal detection, entry tracking from real fills, TP1/TP2/stop/time-stop exits, duplicate exit-order guard.
- **Conflict guard:** MM quoting is paused on an outcome while mispricing trade/order context is active there.
- **Risk controls:** kill switch on daily drawdown / stopout thresholds, pause until next UTC day.

## Inspect DB
```bash
sqlite3 data/bot.sqlite3 '.tables'
sqlite3 data/bot.sqlite3 'select * from pnl_snapshots order by ts desc limit 5;'
sqlite3 data/bot.sqlite3 'select * from daily_metrics order by day_key desc limit 5;'
```

## Stop Bot
```bash
docker compose down
```

## Still required before production deployment
- Endpoint-by-endpoint LIVE contract verification (request/response schema, status enums, signing flow).
- Reconciliation tests for partial fills, cancels, and cancel-all semantics against real venue behavior.
- Dry-run/staging burn-in with alerting and manual supervision.
