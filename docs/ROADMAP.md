# Roadmap

Build from the inside out. Each phase ends with something that runs and is tested.
Mark the current phase with **(current)**.

## Phase 0: Skeleton (done)
- uv project, src/evbot layout, pytest, ruff, mypy, import-linter, config/*.toml loader
- Done when: `pytest`, `ruff`, `mypy`, `lint-imports` all pass on an empty project

## Phase 1: Domain **(current)**
- EarningsEvent, ReportTiming, OptionContract, OptionRight, OptionQuote, OptionChain, Bar, PriceHistory, Leg, CalendarSpread, SignalMetrics, Verdict, Signal, Fill, Position
- Tests: entry_date/exit_date for BMO and AMC, mid, spread_pct, atm_strike, atm_iv, pnl
- Done when: a hand-built OptionChain returns its ATM IV in a test

## Phase 2: Strategy
- TermStructure, YangZhangEstimator, CloseToCloseEstimator, MetricsCalculator, the 4 filters, DefaultVerdictPolicy, SpreadSelector, SignalEvaluator
- Oracle: legacy `calculator.py` metrics on saved chains must match (except the earnings-date fix)
- Done when: evaluate() gives the expected verdict on Recommended / Consider / Avoid fixtures

## Phase 3: Ports + read-only adapters
- Interfaces in ports/; NasdaqEarningsCalendar, AlpacaMarketData, SnapshotStore (files)
- Done when: a script prints tomorrow's real earnings tickers with real metrics

## Phase 4: Scanner + recorder in production
- Scanner, MarketClock, InMemoryTradeRepository, main_live.py running the scan daily (no trading)
- Done when: chains and signals are saved every trading afternoon

## Phase 5: Paper execution
- AlpacaBroker, MidWalkPolicy, ExecutionService, FractionalKellySizer, RiskManager, TradingBot entry + exit jobs
- Done when: one full calendar trade on paper with no human intervention

## Phase 6: Database
- PostgresTradeRepository, schema, migrations, Docker Compose
- Done when: every decision lands in Postgres from the running bot

## Phase 7: Backtester
- SimulatedClock, HistoricalMarketData, SimulatedBroker, Backtester; overlapping trades share capital
- Done when: one command produces an equity curve and stats by year

## Phase 8: Dashboard
- API over Postgres + frontend: equity curve, open positions, today's signals, live vs backtest
- Done when: the day's trading is visible in a browser
