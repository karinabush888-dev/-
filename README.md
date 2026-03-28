# Polymarket CLOB Trading Bot MVP

## Overview
Automated Python 3.11 bot for Polymarket CLOB with PAPER and LIVE modes, SQLite journaling, Telegram reports, risk engine, compounding sizing, Market Making + Mispricing/Event strategies, and UTC daily controls.

## Project Structure
- `app/` bootstrap and main runtime entrypoint
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

## Run
```bash
docker compose up -d --build
```

## PAPER Mode
```bash
MODE=PAPER docker compose up -d --build
```

## LIVE Mode
```bash
MODE=LIVE docker compose up -d --build
```

## Paper → Live Checklist
- Set API keys and passphrase in `.env`
- Set `MODE=LIVE`
- Verify `TELEGRAM_ENABLED` and chat config
- Validate market and outcome selections from startup message
- Keep `CANCEL_ALL_ON_EXIT=true`

## Data and Logs
- SQLite DB: `./data/bot.sqlite3`
- Logs: `./logs/bot.log`

## Outcome Selection
Per configured market, picks exactly one outcome using probability band 30%–70%, max liquidity, tie-break closest to 50%.

## Compounding
All allocations and order sizes are recalculated from current equity after every fill and periodic mark-to-market snapshot.

## Kill Switch
Triggers on daily loss limit or max stopouts/day. Behavior: cancel-all and pause until next UTC day.

## Market Making
One-level bid/ask around mid with spread guard and stale replace via periodic refresh.
Inventory skew and reduce-only enforced by exposure thresholds.

## Mispricing
Signal requires >=10c move within 5m then >=2m stabilization. Uses staged exits TP1/TP2, strict stop -5%, and 20m time-stop.

## Adaptation Modes
- `NORMAL`
- `ACCEL`: +10% MM size for 24h when 3-day pnl strong and stopouts low
- `BRAKE`: -20% sizes for 24h on stopout or 2 negative days

## Inspect DB
```bash
sqlite3 data/bot.sqlite3 '.tables'
sqlite3 data/bot.sqlite3 'select * from pnl_snapshots order by ts desc limit 5;'
```

## Stop Bot
```bash
docker compose down
```
