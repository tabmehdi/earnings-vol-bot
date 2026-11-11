class PortError(Exception):
    """Base for failures of an external system behind a port."""


class EarningsCalendarError(PortError):
    pass


class MarketDataError(PortError):
    pass


class TradingCalendarError(PortError):
    pass


class SnapshotError(PortError):
    pass


class RepositoryError(PortError):
    pass


class RiskFreeRateError(PortError):
    pass
