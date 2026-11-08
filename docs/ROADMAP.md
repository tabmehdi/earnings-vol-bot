# Roadmap

Build from the inside out. Each phase ends with something that runs and is tested.
Mark the current phase with **(current)**.

## Phase 0: Skeleton (done)
- uv project, src/evbot layout, pytest, ruff, mypy, import-linter, config/*.toml loader
- Done when: `pytest`, `ruff`, `mypy`, `lint-imports` all pass on an empty project

## Phase 1: Domain (done)
- EarningsEvent, ReportTiming (BMO, AMC, UNKNOWN), OptionContract, OptionRight, OptionQuote, OptionChain, Bar, PriceHistory, Side, Leg, CalendarSpread, SignalMetrics, FilterResult, Verdict, Signal, Fill, Position
- EarningsEvent is pure data: entry/exit dates need a trading calendar (holidays, early closes), so they are resolved in Phase 3+, not in the domain
- OptionContract carries `multiplier` (default 100)
- Tests: mid, spread_pct, atm_strike, atm_iv, pnl
- Done when: a hand-built OptionChain returns its ATM IV in a test

## Phase 2: Strategy **(current)**
- TermStructure, YangZhangEstimator, CloseToCloseEstimator, MetricsCalculator, the 4 filters, DefaultVerdictPolicy, SpreadSelector, SignalEvaluator
- Oracle: legacy `calculator.py` metrics on saved chains must match (except the earnings-date fix)
- Done when: evaluate() gives the expected verdict on Recommended / Consider / Avoid fixtures

## Phase 3: Ports + read-only adapters
- Interfaces in ports/; NasdaqEarningsCalendar, AlpacaMarketData, SnapshotStore (files)
- TradingCalendar port (previous_session, next_session, close_time) with an Alpaca calendar adapter; Scanner and TradingBot use it to resolve entry/exit sessions from an EarningsEvent
- AlpacaMarketData excludes adjusted / non-standard contracts (multiplier != 100, non-standard deliverables)
- Done when: a script prints tomorrow's real earnings tickers with real metrics

## Phase 4: Scanner + recorder in production
- Scanner, MarketClock, InMemoryTradeRepository, main_live.py running the scan daily (no trading)
- ReportTiming.UNKNOWN produces an AVOID signal with a FilterResult explaining why, never a silent skip
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
