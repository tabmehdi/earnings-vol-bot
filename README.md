# Earnings Volatility Bot

[![CI](https://github.com/tabmehdi/earnings-vol-bot/actions/workflows/ci.yml/badge.svg)](https://github.com/tabmehdi/earnings-vol-bot/actions/workflows/ci.yml)

Event-driven options trading bot that sells earnings IV crush with long calendar spreads, filtering on term-structure slope, IV/RV and liquidity. Built with ports and adapters so the live bot (Alpaca) and the backtester share the same strategy code.

> Paper trading only. Educational project, not investment advice.

## How it works
1. **Scan** (~15:30 ET): fetch upcoming earnings (Nasdaq), each ticker's option chain and price history.
2. **Evaluate**: compute 30-day average volume, IV30/RV30 (Yang-Zhang) and term-structure slope; verdict Recommended / Consider / Avoid.
3. **Trade**: for Recommended signals, size with fractional Kelly under an exposure cap and open an ATM call calendar (front = first expiry after earnings, back about +30 days).
4. **Exit** (~9:45 ET next session): close every open calendar.
5. **Record**: every signal, chain, order and fill is stored for analysis and backtesting.

## Project layout
```
src/evbot/
  domain/     immutable domain objects
  strategy/   metrics, filters, verdict, selection, sizing
  ports/      interfaces to the outside world
  app/        Scanner, ExecutionService, TradingBot
  adapters/   Nasdaq, Alpaca, storage
  backtest/   simulated data, broker and clock + Backtester
```
See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) and [docs/ROADMAP.md](docs/ROADMAP.md).

## Getting started
```bash
uv sync
cp .env.example .env        # ALPACA_API_KEY, ALPACA_SECRET_KEY, ALPACA_PAPER=true
uv run pytest -m "not integration"
```

## Status
Rebuild in progress. See the roadmap for the current phase.
