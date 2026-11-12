from datetime import datetime
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime:
        """Current timezone-aware time."""
        ...

    def wait(self, seconds: float) -> None:
        """Pause; a simulated clock just moves its time forward."""
        ...
