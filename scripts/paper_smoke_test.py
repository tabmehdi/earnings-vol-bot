"""Manual paper smoke test: opens and closes one SPY calendar through the bot's own code.

During market hours it builds a 1-contract SPY ATM call calendar with the usual selection
rules (front = first expiry after today, back = standard monthly near front + 30 days, strike
closest to spot), opens it with ExecutionService.open (mid walk, at most 2 minutes), then
closes it with ExecutionService.close: one mleg order, falling back to buying back the front
and selling the back if Alpaca refuses it. Every order, status and fill is printed.

--dry-run fetches the same chain and quotes and prints the order bodies that would be sent
(the opening walk, the mleg close and the leg-by-leg fallback) without creating a broker.

Paper only: it stops unless ALPACA_PAPER=true, and AlpacaBroker refuses anything but the paper
URL. Records go to data/paper_smoke/, not to the session's folders.

    uv run python scripts/paper_smoke_test.py --dry-run
    uv run python scripts/paper_smoke_test.py
"""

import argparse
import json
import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import requests
from dotenv import load_dotenv

from evbot.adapters.alpaca_broker import AlpacaBroker, order_body
from evbot.adapters.alpaca_market_data import AlpacaMarketData, occ_symbol
from evbot.adapters.alpaca_trading_calendar import AlpacaTradingCalendar
from evbot.adapters.jsonl_trade_repository import JsonlTradeRepository
from evbot.adapters.market_clock import MarketClock
from evbot.app.execution import ExecutionService
from evbot.config import StrategyConfig, load_config
from evbot.domain.events import EarningsEvent, ReportTiming
from evbot.domain.options import OptionContract, OptionQuote
from evbot.domain.orders import OrderIntent, OrderTicket, SpreadLeg
from evbot.domain.snapshots import DataFeeds
from evbot.domain.spreads import CalendarSpread, Side
from evbot.domain.trades import Fill, Position
from evbot.main_live import build_http
from evbot.strategy.expiries import monthly_expiries
from evbot.strategy.pricing import MidWalkPolicy
from evbot.strategy.selection import SpreadSelector

SYMBOL = "SPY"
QUANTITY = 1
OPEN_WAIT = timedelta(minutes=2)
CLOSE_WAIT = timedelta(minutes=4)  # room for two walks if the close has to leg out
RECORDS = Path("data/paper_smoke")
CLOCK = MarketClock()


def show(text: str) -> None:
    print(f"{CLOCK.now():%H:%M:%S}  {text}", flush=True)


def describe(ticket: OrderTicket) -> str:
    what = "calendar" if ticket.leg is None else f"{ticket.leg.value} leg"
    fill = (
        f", filled {ticket.filled_quantity} @ {ticket.fill_price:.2f}" if ticket.fill_price else ""
    )
    return (
        f"{ticket.client_order_id} [{ticket.order_id}] {ticket.intent.value} {what} "
        f"x{ticket.quantity} limit {ticket.limit_price:.2f}: {ticket.status.value}{fill}"
    )


def describe_fill(name: str, fill: Fill | None) -> str:
    if fill is None:
        return f"{name}: none"
    markets = ", ".join(
        f"{side} {m.bid:.2f}/{m.ask:.2f}"
        for side, m in (("front", fill.front), ("back", fill.back))
        if m is not None
    )
    return (
        f"{name}: {fill.net_price:+.2f} x{fill.quantity} at {fill.filled_at:%H:%M:%S} ({markets})"
    )


@dataclass
class PrintingBroker:
    """The real broker, printing every call and every ticket it returns."""

    inner: AlpacaBroker

    def account_equity(self) -> float:
        return self.inner.account_equity()

    def submit(
        self,
        spread: CalendarSpread,
        intent: OrderIntent,
        quantity: int,
        limit_price: float,
        client_order_id: str,
        leg: SpreadLeg | None = None,
    ) -> OrderTicket:
        what = "calendar" if leg is None else f"{leg.value} leg"
        show(f"submit {intent.value} {what} x{quantity} at {limit_price:.2f} ({client_order_id})")
        ticket = self.inner.submit(spread, intent, quantity, limit_price, client_order_id, leg)
        show(f"  -> {describe(ticket)}")
        return ticket

    def order(self, ticket: OrderTicket) -> OrderTicket:
        current = self.inner.order(ticket)
        show(f"  status {describe(current)}")
        return current

    def find_order(self, client_order_id: str, spread: CalendarSpread) -> OrderTicket | None:
        return self.inner.find_order(client_order_id, spread)

    def replace(self, ticket: OrderTicket, limit_price: float) -> OrderTicket:
        show(f"replace {ticket.order_id} -> {limit_price:.2f}")
        current = self.inner.replace(ticket, limit_price)
        show(f"  -> {describe(current)}")
        return current

    def cancel(self, order_id: str) -> None:
        show(f"cancel {order_id}")
        self.inner.cancel(order_id)

    def held_contracts(self) -> Mapping[OptionContract, int]:
        return self.inner.held_contracts()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--dry-run", action="store_true", help="print the orders without sending them"
    )
    args = parser.parse_args()

    load_dotenv()
    key_id = os.environ.get("ALPACA_API_KEY")
    secret_key = os.environ.get("ALPACA_SECRET_KEY")
    if not key_id or not secret_key:
        print("set ALPACA_API_KEY and ALPACA_SECRET_KEY in .env")
        return 2
    if os.environ.get("ALPACA_PAPER", "").lower() != "true":
        print("paper only: set ALPACA_PAPER=true")
        return 2

    config = load_config()
    market_config = config.market_data
    with requests.Session() as session:
        http = build_http(market_config, session, market_config.alpaca_requests_per_minute)
        calendar = AlpacaTradingCalendar(http, market_config.trading_base_url, key_id, secret_key)
        market = AlpacaMarketData(
            http,
            CLOCK,
            market_config.data_base_url,
            market_config.trading_base_url,
            key_id,
            secret_key,
            feeds=DataFeeds(
                market_config.options_feed, market_config.spot_feed, market_config.history_feed
            ),
            chain_max_days=market_config.chain_max_days,
            strike_range_pct=market_config.strike_range_pct,
            max_quote_age_seconds=market_config.max_quote_age_seconds,
        )

        now = CLOCK.now()
        today = now.date()
        if today not in calendar.sessions_between(today, today):
            print(f"{today} is not a trading session")
            return 1
        close_at = calendar.close_time(today)
        in_hours = calendar.open_time(today) <= now <= close_at - timedelta(minutes=10)
        if not in_hours and not args.dry_run:
            print(f"run between the open and 10 minutes before the close ({close_at:%H:%M})")
            return 1

        spread = choose_spread(config, calendar, market, today)
        if spread is None:
            return 1
        quotes = market.get_leg_quotes((spread.front.contract, spread.back.contract))
        for contract in (spread.front.contract, spread.back.contract):
            q = quotes[contract]
            show(f"  {occ_symbol(contract)} bid {q.bid:.2f} ask {q.ask:.2f} mid {q.mid:.2f}")

        execution_config = config.execution
        pricing = MidWalkPolicy(
            execution_config.walk_steps,
            execution_config.walk_step_seconds,
            execution_config.entry_cap_fraction,
        )
        tag = f"smoke-{now:%Y%m%d-%H%M%S}"
        if args.dry_run:
            print_orders(spread, quotes, pricing, tag)
            show(f"dry run: nothing sent ({http.request_count} read requests)")
            return 0

        broker = PrintingBroker(
            AlpacaBroker(
                http, CLOCK, market_config.trading_base_url, key_id, secret_key, paper=True
            )
        )
        repository = JsonlTradeRepository(RECORDS / "signals", RECORDS / "trades")
        execution = ExecutionService(broker, market, pricing, repository, CLOCK)
        result = round_trip(execution, broker, spread, tag)
        show(f"{http.request_count} requests; records in {RECORDS}")
    return result


def choose_spread(
    config: StrategyConfig,
    calendar: AlpacaTradingCalendar,
    market: AlpacaMarketData,
    today: date,
) -> CalendarSpread | None:
    snapshot = market.get_chain(SYMBOL)
    horizon = today + timedelta(days=config.market_data.chain_max_days)
    next_month = (horizon.replace(day=28) + timedelta(days=4)).replace(day=1)
    sessions = set(calendar.sessions_between(today, next_month))
    monthlies = monthly_expiries(today, horizon, sessions.__contains__)
    # As if SPY reported after today's close: the front is the first expiry after today.
    event = EarningsEvent(SYMBOL, today, ReportTiming.AMC)
    structure = config.structure
    choice = SpreadSelector(structure.back_target_days, structure.back_min_gap_days).select(
        snapshot.chain, event, monthlies
    )
    if choice.spread is None:
        print(f"no spread: {choice.note}")
        return None
    spread = choice.spread
    show(
        f"spread: spot {snapshot.chain.spot:.2f}, sell {occ_symbol(spread.front.contract)}, "
        f"buy {occ_symbol(spread.back.contract)} ({snapshot.stale_quotes_dropped} stale dropped)"
    )
    return spread


def print_orders(
    spread: CalendarSpread,
    quotes: Mapping[OptionContract, OptionQuote],
    pricing: MidWalkPolicy,
    tag: str,
) -> None:
    """The bodies the real run would POST, with the walk's later prices (sent as PATCHes)."""
    front, back = quotes[spread.front.contract], quotes[spread.back.contract]
    steps = range(pricing.steps)

    def body(intent: OrderIntent, price: float, cid: str, leg: SpreadLeg | None = None) -> None:
        print(json.dumps(order_body(spread, intent, QUANTITY, price, cid, leg), indent=2))

    opening = [pricing.limit(OrderIntent.OPEN, a, front, back) for a in steps]
    show(f"== open: calendar walk {opening}")
    body(OrderIntent.OPEN, opening[0], f"{tag}-open")

    closing = [pricing.limit(OrderIntent.CLOSE, a, front, back) for a in steps]
    natural = pricing.natural(OrderIntent.CLOSE, front, back)
    show(f"== close: calendar walk {closing}, then natural {natural:.2f}")
    body(OrderIntent.CLOSE, closing[0], f"{tag}-close")

    buy_front = [pricing.leg_limit(Side.BUY, a, front) for a in steps]
    sell_back = [pricing.leg_limit(Side.SELL, a, back) for a in steps]
    show(f"== if the calendar close is refused: buy back the front, walk {buy_front}")
    body(OrderIntent.CLOSE, buy_front[0], f"{tag}-close-front", SpreadLeg.FRONT)
    show(f"== then sell the back, walk {sell_back}")
    body(OrderIntent.CLOSE, sell_back[0], f"{tag}-close-back", SpreadLeg.BACK)


def round_trip(
    execution: ExecutionService, broker: PrintingBroker, spread: CalendarSpread, tag: str
) -> int:
    show(f"paper equity {broker.account_equity():,.2f}")
    show(f"held option contracts before: {dict(broker.held_contracts()) or 'none'}")

    show("== open")
    position = execution.open(
        spread, QUANTITY, f"{tag}-open", CLOCK.now() + OPEN_WAIT, CLOCK.now() + OPEN_WAIT
    )
    if position is None:
        show("open not filled within 2 minutes: nothing to close")
        return 1
    show(describe_fill("entry", position.entry))

    remaining: Position | None = position
    closed: Position | None = None
    for attempt, at_natural in (("close", False), ("close-nat", True)):
        assert remaining is not None
        state = "long back only" if remaining.long_back_only else "calendar"
        show(f"== {attempt} ({state}{', at natural' if at_natural else ''})")
        closed, remaining = execution.close(
            remaining, f"{tag}-{attempt}", CLOCK.now() + CLOSE_WAIT, at_natural
        )
        if remaining is None:
            break

    if closed is not None:
        show(describe_fill("front buy-back", closed.front_closed))
        show(describe_fill("exit", closed.exit))
        show(f"P&L {closed.pnl():+.2f}")
    if remaining is not None:
        state = "long back only" if remaining.long_back_only else "the calendar"
        show(f"NOT CLOSED: {state} is still open; close it in the Alpaca paper dashboard")
    show(f"held option contracts after: {dict(broker.held_contracts()) or 'none'}")
    return 0 if remaining is None else 1


if __name__ == "__main__":
    sys.exit(main())
