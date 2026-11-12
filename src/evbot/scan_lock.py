import os
from pathlib import Path


class ScanLock:
    """A lock file created atomically, so two scans can never run at once.

    The file is named per day by the caller and holds the owner's process id. On Linux a
    lock whose process is gone (a crash, a kill) is taken over, so a restarted process is
    not locked out; elsewhere it blocks the rest of that day and the log names the file.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._held = False

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            if not self._owner_is_gone():
                return False
            self.path.unlink(missing_ok=True)
            return self.acquire()
        with os.fdopen(descriptor, "w") as lock_file:
            lock_file.write(str(os.getpid()))
        self._held = True
        return True

    def _owner_is_gone(self) -> bool:
        if os.name != "posix":
            return False  # no reliable liveness check without extra dependencies
        try:
            owner = int(self.path.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            return False
        try:
            os.kill(owner, 0)  # signal 0: only checks that the process exists
        except ProcessLookupError:
            return True
        except PermissionError:
            return False
        return False

    def release(self) -> None:
        if self._held:
            self.path.unlink(missing_ok=True)
            self._held = False
