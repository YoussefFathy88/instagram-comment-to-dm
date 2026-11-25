"""Single-run lock file, so a scheduled run and a manual/dashboard run never overlap."""

import os
import time
from pathlib import Path

from app.domain.errors import DomainError


class RunLocked(DomainError):
    pass


class RunLock:
    def __init__(self, path: Path, stale_after_s: float = 2 * 3600):
        self.path = path
        self.stale_after_s = stale_after_s

    def __enter__(self) -> "RunLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists() and time.time() - self.path.stat().st_mtime > self.stale_after_s:
            self.path.unlink(missing_ok=True)  # left over from a crashed run
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            raise RunLocked(f"Another engagement run is in progress ({self.path.name}). Try again later.") from None
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return self

    def __exit__(self, *exc) -> None:
        self.path.unlink(missing_ok=True)
