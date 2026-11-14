import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

from evbot.domain.options import OptionContract, OptionQuote
from evbot.domain.orders import OrderIntent, OrderStatus, OrderTicket, SpreadLeg
from evbot.domain.signals import LegPrices
from evbot.domain.spreads import CalendarSpread, Leg, Side
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
    quotes: Quotes | None  # the market of the traded contracts when the fill was seen


@dataclass(frozen=True, slots=True)
class ExecutionService:
    """Works one order at a time: fresh quotes, a limit from the pricing policy, re-priced
    every step until filled, out of steps or past the deadline, then cancelled.

    A close goes out as one calendar (mleg) order. If the broker refuses it, the calendar is
    closed leg by leg: first the short front is bought back (that removes the assignment
    risk), then the long back is sold, each with its own walk. A position whose front is
    bought back but whose back is not sold yet is "long back only"; closing it again only
    sells the back.
    """

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
        filled = self._calendar_fill(outcome)
        if filled is None:
            return None
        position = Position(spread, filled, position_id=client_order_id, exit_at=exit_at)
        self.repository.save_position(position)
        return position

    def close(
        self, position: Position, client_order_id: str, deadline: datetime, at_natural: bool
    ) -> tuple[Position | None, Position | None]:
        """(the part closed, the part still open); either may be None.

        After a partial legged exit two parts can stay open; both are saved, and the one
        returned is the calendar part, because its short front still carries the risk.
        """
        if position.long_back_only:
            return self._sell_back(position, client_order_id, deadline, at_natural)
        quantity = position.entry.quantity
        outcome = self._work(
            position.spread, OrderIntent.CLOSE, quantity, client_order_id, deadline, at_natural
        )
        if outcome.ticket is not None and outcome.ticket.status is OrderStatus.REJECTED:
            logger.warning(
                "%s: the calendar close was refused; closing leg by leg", client_order_id
            )
            return self._leg_out(position, client_order_id, deadline, at_natural)
        filled = self._calendar_fill(outcome)
        if filled is None:
            return None, position
        return self._closed(position, filled)

    # Legging out

    def _leg_out(
        self, position: Position, client_order_id: str, deadline: datetime, at_natural: bool
    ) -> tuple[Position | None, Position | None]:
        quantity = position.entry.quantity
        outcome = self._work(
            position.spread,
            OrderIntent.CLOSE,
            quantity,
            f"{client_order_id}-front",
            deadline,
            at_natural,
            SpreadLeg.FRONT,
        )
        bought = self._leg_fill(outcome, position.spread.front.contract)
        if bought is None:
            logger.warning("%s: the front was not bought back", client_order_id)
            return None, position
        calendar_rest = None
        if bought.quantity < quantity:
            position, calendar_rest = position.split(bought.quantity)
            self.repository.save_position(calendar_rest)
        back_only = position.close_front(bought)
        self.repository.save_position(back_only)
        logger.info(
            "%s: front bought back at %.2f; selling the back", client_order_id, bought.net_price
        )
        closed, back_rest = self._sell_back(back_only, client_order_id, deadline, at_natural)
        return closed, calendar_rest or back_rest

    def _sell_back(
        self, position: Position, client_order_id: str, deadline: datetime, at_natural: bool
    ) -> tuple[Position | None, Position | None]:
        bought = position.front_closed
        assert bought is not None
        quantity = position.entry.quantity
        outcome = self._work(
            position.spread,
            OrderIntent.CLOSE,
            quantity,
            f"{client_order_id}-back",
            deadline,
            at_natural,
            SpreadLeg.BACK,
        )
        sold = self._leg_fill(outcome, position.spread.back.contract)
        if sold is None:
            logger.warning("%s: the back was not sold; long back only", client_order_id)
            return None, position
        # The exit is the whole calendar's: back sale minus front buy-back, both legs' markets.
        exit_fill = Fill(
            sold.net_price - bought.net_price,
            sold.quantity,
            sold.filled_at,
            front=bought.front,
            back=sold.back,
        )
        return self._closed(position, exit_fill)

    def _closed(
        self, position: Position, exit_fill: Fill
    ) -> tuple[Position | None, Position | None]:
        if exit_fill.quantity == position.entry.quantity:
            closed = position.close(exit_fill)
            self.repository.save_position(closed)
            return closed, None
        part, rest = position.split(exit_fill.quantity)
        closed = part.close(exit_fill)
        self.repository.save_position(closed)
        self.repository.save_position(rest)
        return closed, rest

    # Working one order

    def _work(
        self,
        spread: CalendarSpread,
        intent: OrderIntent,
        quantity: int,
        client_order_id: str,
        deadline: datetime,
        at_natural: bool,
        leg: SpreadLeg | None = None,
    ) -> _Outcome:
        ticket = self.broker.find_order(client_order_id, spread)
        if ticket is not None and ticket.is_done:
            # This order already ran (e.g. before a restart); never send it twice.
            return _Outcome(ticket, None)
        contracts = _traded(spread, leg)
        quotes: Quotes | None = None
        for attempt in range(1 if at_natural else self.pricing.steps):
            if self.clock.now() >= deadline:
                logger.info("%s: deadline %s reached", client_order_id, deadline)
                break
            try:
                quotes = self.market_data.get_leg_quotes(contracts)
            except PortError as error:
                logger.warning("%s: no leg quotes (%s); skipping a step", client_order_id, error)
                self._pause(deadline)
                continue
            price = self._price(spread, intent, leg, attempt, at_natural, quotes)
            if ticket is None:
                ticket = self.broker.submit(
                    spread, intent, quantity, price, client_order_id, leg=leg
                )
            else:
                ticket = self.broker.replace(ticket, price)
            self.repository.save_order(ticket)
            if ticket.status is OrderStatus.REJECTED:
                logger.warning("%s: rejected at %.2f", client_order_id, price)
                return _Outcome(ticket, None)
            self._pause(deadline)
            ticket = self._check(ticket)
            if ticket.status is OrderStatus.FILLED:
                return _Outcome(ticket, self._quotes_or(contracts, quotes))
            if ticket.is_done:
                # Refused after it was accepted, or cancelled outside the bot: never re-price.
                logger.warning("%s: order ended as %s", client_order_id, ticket.status.value)
                filled = ticket.filled_quantity > 0
                return _Outcome(ticket, self._quotes_or(contracts, quotes) if filled else None)
        if ticket is not None and not ticket.is_done:
            self.broker.cancel(ticket.order_id)
            ticket = self._check(ticket)
            logger.info(
                "%s: cancelled the working order (%s)", client_order_id, ticket.status.value
            )
        filled_quotes = (
            self._quotes_or(contracts, quotes) if ticket and ticket.filled_quantity else None
        )
        return _Outcome(ticket, filled_quotes)

    def _price(
        self,
        spread: CalendarSpread,
        intent: OrderIntent,
        leg: SpreadLeg | None,
        attempt: int,
        at_natural: bool,
        quotes: Quotes,
    ) -> float:
        if leg is None:
            front, back = quotes[spread.front.contract], quotes[spread.back.contract]
            if at_natural:
                return self.pricing.natural(intent, front, back)
            return self.pricing.limit(intent, attempt, front, back)
        traded = _leg(spread, leg)
        side = traded.side if intent is OrderIntent.OPEN else _opposite(traded.side)
        quote = quotes[traded.contract]
        if at_natural:
            return self.pricing.leg_natural(side, quote)
        return self.pricing.leg_limit(side, attempt, quote)

    def _check(self, ticket: OrderTicket) -> OrderTicket:
        current = self.broker.order(ticket)
        if current != ticket:
            self.repository.save_order(current)
        return current

    def _pause(self, deadline: datetime) -> None:
        remaining = (deadline - self.clock.now()).total_seconds()
        if remaining > 0:
            self.clock.wait(min(self.pricing.step_seconds, remaining))

    def _quotes_or(
        self, contracts: tuple[OptionContract, ...], fallback: Quotes | None
    ) -> Quotes | None:
        try:
            return self.market_data.get_leg_quotes(contracts)
        except PortError:
            return fallback

    # Fills

    def _calendar_fill(self, outcome: _Outcome) -> Fill | None:
        ticket = outcome.ticket
        if ticket is None or ticket.filled_quantity == 0 or ticket.fill_price is None:
            return None
        front = back = None
        if outcome.quotes is not None:
            front = _leg_prices(outcome.quotes[ticket.spread.front.contract])
            back = _leg_prices(outcome.quotes[ticket.spread.back.contract])
        return Fill(
            ticket.fill_price, ticket.filled_quantity, self.clock.now(), front=front, back=back
        )

    def _leg_fill(self, outcome: _Outcome, contract: OptionContract) -> Fill | None:
        """One leg's fill, its market kept on the side it trades (front or back)."""
        ticket = outcome.ticket
        if ticket is None or ticket.filled_quantity == 0 or ticket.fill_price is None:
            return None
        market = None
        if outcome.quotes is not None:
            market = _leg_prices(outcome.quotes[contract])
        is_front = contract == ticket.spread.front.contract
        return Fill(
            ticket.fill_price,
            ticket.filled_quantity,
            self.clock.now(),
            front=market if is_front else None,
            back=None if is_front else market,
        )


def _traded(spread: CalendarSpread, leg: SpreadLeg | None) -> tuple[OptionContract, ...]:
    """The contracts an order trades: both for a calendar, only its own for a leg (an
    expired front has no quote any more, and a back sale does not need one)."""
    if leg is None:
        return (spread.front.contract, spread.back.contract)
    return (_leg(spread, leg).contract,)


def _leg(spread: CalendarSpread, leg: SpreadLeg) -> Leg:
    return spread.front if leg is SpreadLeg.FRONT else spread.back


def _opposite(side: Side) -> Side:
    return Side.BUY if side is Side.SELL else Side.SELL


def _leg_prices(quote: OptionQuote) -> LegPrices:
    return LegPrices(quote.bid, quote.ask, quote.mid, None, quote.quoted_at)
