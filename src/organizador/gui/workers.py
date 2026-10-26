"""Tareas en segundo plano con ``QThreadPool``.

Los resultados vuelven al hilo de la interfaz a través de un ``_Bridge`` creado en él:
en PySide6 una señal conectada a una función suelta (sin ``QObject`` receptor) se ejecuta
en el hilo que la emite, y tocar widgets desde un hilo de trabajo rompe Qt.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal, Slot

log = logging.getLogger(__name__)

Progress = Callable[[Any], None]


class _Bridge(QObject):
    raw_done = Signal(object)
    raw_failed = Signal(object)
    raw_progress = Signal(object)

    def __init__(
        self,
        on_done: Callable[[Any], object] | None,
        on_error: Callable[[Exception], object] | None,
        on_progress: Callable[[Any], object] | None,
        finished: Callable[[_Bridge], None],
    ) -> None:
        super().__init__()
        self._on_done = on_done
        self._on_error = on_error
        self._on_progress = on_progress
        self._finished = finished
        self.raw_done.connect(self._done)
        self.raw_failed.connect(self._failed)
        self.raw_progress.connect(self._progress)

    @Slot(object)
    def _done(self, result: Any) -> None:
        try:
            if self._on_done is not None:
                self._on_done(result)
        finally:
            self._finished(self)

    @Slot(object)
    def _failed(self, exc: Exception) -> None:
        try:
            if self._on_error is not None:
                self._on_error(exc)
            else:
                log.error("Tarea fallida: %s", exc)
        finally:
            self._finished(self)

    @Slot(object)
    def _progress(self, value: Any) -> None:
        if self._on_progress is not None:
            self._on_progress(value)


class _Task(QRunnable):
    def __init__(self, fn: Callable[..., Any], bridge: _Bridge, with_progress: bool) -> None:
        super().__init__()
        self._fn = fn
        self._bridge = bridge
        self._with_progress = with_progress

    def run(self) -> None:
        try:
            args = (self._bridge.raw_progress.emit,) if self._with_progress else ()
            result = self._fn(*args)
        except Exception as exc:
            log.debug("Tarea fallida", exc_info=True)
            self._bridge.raw_failed.emit(exc)
        else:
            self._bridge.raw_done.emit(result)


class Runner:
    """Lanza funciones en el pool y entrega el resultado en el hilo de la interfaz."""

    def __init__(self, pool: QThreadPool | None = None) -> None:
        self.pool = pool or QThreadPool.globalInstance()
        self._alive: set[_Bridge] = set()
        self._lock = threading.Lock()

    def submit(
        self,
        fn: Callable[..., Any],
        *,
        on_done: Callable[[Any], object] | None = None,
        on_error: Callable[[Exception], object] | None = None,
        on_progress: Callable[[Any], object] | None = None,
    ) -> None:
        """``fn()`` en segundo plano; con ``on_progress``, se llama ``fn(progress)``."""
        bridge = _Bridge(on_done, on_error, on_progress, self._release)
        with self._lock:
            self._alive.add(bridge)  # sin referencia, Python lo recogería antes de tiempo
        self.pool.start(_Task(fn, bridge, on_progress is not None))

    def _release(self, bridge: _Bridge) -> None:
        with self._lock:
            self._alive.discard(bridge)

    @property
    def busy(self) -> int:
        with self._lock:
            return len(self._alive)
