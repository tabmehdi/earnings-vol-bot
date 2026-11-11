import os
from pathlib import Path


class ScanLock:
    """A lock file created atomically, so two scans can never run at once.

    The file is named per day by the caller. A scan that crashes without releasing it only
    blocks the rest of that day, and the log says which file to remove.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._held = False

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            return False
        with os.fdopen(descriptor, "w") as lock_file:
            lock_file.write(str(os.getpid()))
        self._held = True
        return True

    def release(self) -> None:
        if self._held:
            self.path.unlink(missing_ok=True)
            self._held = False
