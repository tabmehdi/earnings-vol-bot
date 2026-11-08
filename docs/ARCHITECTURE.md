# Architecture

Ports and adapters. The strategy knows nothing about Nasdaq, Alpaca or the database.
Live trading and backtesting build the same `TradingBot` with different adapters.

Diagrams: https://claude.ai/artifact/RR1KS8V1bvHv3iARM3CDf6

## Layers
| Folder | Contains | May import |
|---|---|---|
| domain/ | Immutable data: EarningsEvent, OptionChain, CalendarSpread, Signal, Position... | stdlib only |
| strategy/ | MetricsCalculator, TermStructure, VolatilityEstimator, filters, VerdictPolicy, SpreadSelector, SignalEvaluator, PositionSizer, RiskManager | domain |
| ports/ | Interfaces: EarningsCalendar, MarketDataProvider, Broker, Clock, TradeRepository, SnapshotStore, PricingPolicy | domain |
| app/ | Scanner, ExecutionService, TradingBot | domain, strategy, ports |
| adapters/ | NasdaqEarningsCalendar, AlpacaMarketData, AlpacaBroker, MarketClock, Postgres/Parquet stores | domain, ports, SDKs |
| backtest/ | HistoricalMarketData, SimulatedBroker, SimulatedClock, Backtester | domain, strategy, ports, app |

`main_live.py` and `main_backtest.py` are the composition roots: the only files that create concrete adapters and wire them.

## Data flow (entry job, ~15:30)
1. `EarningsCalendar.events_for(today)` -> AMC today + BMO tomorrow
2. For each event: `MarketDataProvider.get_history` + `get_chain` -> `SnapshotStore.save_chain`
3. `SignalEvaluator.evaluate(MarketContext)` -> Signal (metrics, filter results, verdict, spread)
4. `TradeRepository.save_signal` for every signal
5. RECOMMENDED only: `PositionSizer.size` -> `RiskManager.approve` -> `ExecutionService.open`
6. `ExecutionService`: fresh leg quotes -> submit/replace with `PricingPolicy` until filled or max attempts -> Position

Exit job (~9:45 next session): `TradeRepository.open_positions` -> `ExecutionService.close` -> save.

## Storage
- TradeRepository: decisions and executions (signals, filter results, orders, fills, positions). Relational.
- SnapshotStore: raw market data used for decisions (chains, price history). Columnar files; feeds the backtester.

## Decisions
Architecture decisions are recorded in docs/decisions/NNNN-title.md (context, decision, consequences).
