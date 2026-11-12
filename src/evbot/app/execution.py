import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

from evbot.domain.options import OptionContract, OptionQuote
from evbot.domain.orders import OrderIntent, OrderStatus, OrderTicket
from evbot.domain.signals import LegPrices
from evbot.domain.spreads import CalendarSpread
from evbot.domain.trades import Fill, Position
from evbot.ports.broker import Broker
from evbot.ports.clock import Clock
from evbot.ports.errors import PortError
from evbot.ports.market_data import MarketDataProvider
from evbot.ports.trade_repository import TradeRepository
from evbot.strategy.pricing import PricingPolicy

logger = logging.getLogger(__name__)

Quotes = Mapping[OptionContract, OptionQuote]


@dataclass(frozen=True, slots=True)
class _Outcome:
    ticket: OrderTicket | None
    quotes: Quotes | None  # the legs' market when the fill was seen


@dataclass(frozen=True, slots=True)
class ExecutionService:
    """Works one calendar order at a time: fresh quotes, a limit from the pricing policy,
    re-priced every step until filled, out of steps or past the deadline, then cancelled."""

    broker: Broker
    market_data: MarketDataProvider
    pricing: PricingPolicy
    repository: TradeRepository
    clock: Clock

    def open(
        self,
        spread: CalendarSpread,
        quantity: int,
        client_order_id: str,
        deadline: datetime,
        exit_at: datetime,
    ) -> Position | None:
        already = [p for p in self.repository.open_positions() if p.position_id == client_order_id]
        if already:
            return already[0]
        outcome = self._work(spread, OrderIntent.OPEN, quantity, client_order_id, deadline, False)
        filled = self._fill(outcome)
        if filled is None:
            return None
        position = Position(spread, filled, position_id=client_order_id, exit_at=exit_at)
        self.repository.save_position(position)
        return position

    def close(
        self, position: Position, client_order_id: str, deadline: datetime, at_natural: bool
    ) -> tuple[Position | None, Position | None]:
        """(the part closed, the part still open); either may be None."""
        quantity = position.entry.quantity
        outcome = self._work(
            position.spread, OrderIntent.CLOSE, quantity, client_order_id, deadline, at_natural
        )
        filled = self._fill(outcome)
        if filled is None:
            return None, position
        if filled.quantity == quantity:
            closed = position.close(filled)
            self.repository.save_position(closed)
            return closed, None
        part, rest = position.split(filled.quantity)
        closed = part.close(filled)
        self.repository.save_position(closed)
        self.repository.save_position(rest)
        return closed, rest

    def _work(
        self,
        spread: CalendarSpread,
        intent: OrderIntent,
        quantity: int,
        client_order_id: str,
        deadline: datetime,
        at_natural: bool,
    ) -> _Outcome:
        ticket = self.broker.find_order(client_order_id)
        if ticket is not None and ticket.is_done:
            # This order already ran (e.g. before a restart); never send it twice.
            return _Outcome(ticket, None)
        quotes: Quotes | None = None
        for attempt in range(1 if at_natural else self.pricing.steps):
            if self.clock.now() >= deadline:
                logger.info("%s: deadline %s reached", client_order_id, deadline)
                break
            try:
                quotes = self._quotes(spread)
            except PortError as error:
                logger.warning("%s: no leg quotes (%s); skipping a step", client_order_id, error)
                self._pause(deadline)
                continue
            front, back = quotes[spread.front.contract], quotes[spread.back.contract]
            price = (
                self.pricing.natural(intent, front, back)
                if at_natural
                else self.pricing.limit(intent, attempt, front, back)
            )
            if ticket is None:
                ticket = self.broker.submit(spread, intent, quantity, price, client_order_id)
            else:
                ticket = self.broker.replace(ticket.order_id, price)
            self.repository.save_order(ticket)
            if ticket.status is OrderStatus.REJECTED:
                logger.warning("%s: rejected at %.2f", client_order_id, price)
                return _Outcome(ticket, None)
            self._pause(deadline)
            ticket = self._check(ticket)
            if ticket.status is OrderStatus.FILLED:
                return _Outcome(ticket, self._quotes_or(spread, quotes))
        if ticket is not None and not ticket.is_done:
            self.broker.cancel(ticket.order_id)
            ticket = self._check(ticket)
            logger.info(
                "%s: cancelled the working order (%s)", client_order_id, ticket.status.value
            )
        filled_quotes = (
            self._quotes_or(spread, quotes) if ticket and ticket.filled_quantity else None
        )
        return _Outcome(ticket, filled_quotes)

    def _check(self, ticket: OrderTicket) -> OrderTicket:
        current = self.broker.order(ticket.order_id)
        if current != ticket:
            self.repository.save_order(current)
        return current

    def _pause(self, deadline: datetime) -> None:
        remaining = (deadline - self.clock.now()).total_seconds()
        if remaining > 0:
            self.clock.wait(min(self.pricing.step_seconds, remaining))

    def _quotes(self, spread: CalendarSpread) -> Quotes:
        return self.market_data.get_leg_quotes((spread.front.contract, spread.back.contract))

    def _quotes_or(self, spread: CalendarSpread, fallback: Quotes | None) -> Quotes | None:
        try:
            return self._quotes(spread)
        except PortError:
            return fallback

    def _fill(self, outcome: _Outcome) -> Fill | None:
        ticket = outcome.ticket
        if ticket is None or ticket.filled_quantity == 0 or ticket.fill_price is None:
            return None
        front = back = None
        if outcome.quotes is not None:
            front, back = (_leg_prices(q) for q in _legs(ticket.spread, outcome.quotes))
        return Fill(
            ticket.fill_price, ticket.filled_quantity, self.clock.now(), front=front, back=back
        )


def _legs(spread: CalendarSpread, quotes: Quotes) -> tuple[OptionQuote, OptionQuote]:
    return quotes[spread.front.contract], quotes[spread.back.contract]


def _leg_prices(quote: OptionQuote) -> LegPrices:
    return LegPrices(quote.bid, quote.ask, quote.mid, None, quote.quoted_at)
