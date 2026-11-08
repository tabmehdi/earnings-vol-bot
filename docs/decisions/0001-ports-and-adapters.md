# 0001: Ports and adapters, one core for live and backtest

## Context
The first version mixed API calls, filters and order logic. A separate backtest script would drift from the live logic.

## Decision
Strategy and services depend only on interfaces in ports/. Live and simulated implementations live in adapters/ and backtest/. Time comes from a Clock port.

## Consequences
- The backtester reuses TradingBot unchanged, so backtest results describe the real bot.
- Every external system needs an adapter and an integration test.
- Slightly more files up front.
