from datetime import datetime
from zoneinfo import ZoneInfo

NEW_YORK = ZoneInfo("America/New_York")


class MarketClock:
    def now(self) -> datetime:
        return datetime.now(NEW_YORK)
