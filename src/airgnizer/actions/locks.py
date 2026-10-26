"""Exclusión mutua de operaciones sobre archivos.

- Entre hilos del mismo proceso: bloqueo por ruta, no bloqueante (la segunda
  operación sobre la misma ruta se rechaza).
- Entre procesos (CLI, GUI, daemon): ``flock`` sobre un archivo de bloqueo que
  serializa las operaciones del journal.
"""

from __future__ import annotations

import fcntl
import os
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from airgnizer.actions.errors import ConcurrentOperationError


class PathLocks:
    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._held: set[Path] = set()

    @contextmanager
    def hold(self, *paths: Path) -> Iterator[None]:
        wanted = {p.absolute() for p in paths}
        with self._guard:
            busy = wanted & self._held
            if busy:
                raise ConcurrentOperationError(next(iter(busy)))
            self._held |= wanted
        try:
            yield
        finally:
            with self._guard:
                self._held -= wanted


@contextmanager
def process_lock(lock_file: Path, timeout: float = 30.0) -> Iterator[None]:
    """Bloqueo exclusivo entre procesos; espera hasta ``timeout`` segundos."""
    lock_file.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(lock_file, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise ConcurrentOperationError(lock_file) from None
                time.sleep(0.05)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
