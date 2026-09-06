from __future__ import annotations

import fcntl
import os
from pathlib import Path
from typing import TextIO


class AlreadyRunningError(RuntimeError):
    """Another bot process already owns the persistent runtime lock."""


class RuntimeSingletonLock:
    def __init__(self, state_path: str):
        state = Path(state_path)
        self.path = state.parent / ".binance_futures_bot.lock"
        self._handle: TextIO | None = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+", encoding="utf-8")
        os.chmod(self.path, 0o600)
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            handle.close()
            raise AlreadyRunningError(
                f"Другой экземпляр бота уже использует {self.path}"
            ) from exc
        handle.seek(0)
        handle.truncate()
        handle.write(f"{os.getpid()}\n")
        handle.flush()
        os.fsync(handle.fileno())
        self._handle = handle

    def release(self) -> None:
        if self._handle is None:
            return
        fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        self._handle.close()
        self._handle = None

    def __enter__(self) -> "RuntimeSingletonLock":
        self.acquire()
        return self

    def __exit__(self, *_: object) -> None:
        self.release()
