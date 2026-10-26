"""Tareas en segundo plano con ``QThreadPool``.

Los resultados vuelven al hilo de la interfaz a través de un ``_Hub`` creado en él: en
PySide6 una señal conectada a una función suelta (sin ``QObject`` receptor) se ejecuta en
el hilo que la emite, y tocar widgets desde un hilo de trabajo rompe Qt.
"""

from __future__ import annotations

import itertools
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal, Slot

log = logging.getLogger(__name__)

Progress = Callable[[Any], None]


class _Hub(QObject):
    """Entrega los resultados de todas las tareas de un ``Runner`` en el hilo de la GUI.

    Uno por ``Runner`` y vive tanto como él: las tareas solo guardan su número. Un
    ``QObject`` por tarea podía acabar destruido en el hilo de trabajo (al soltarlo el
    pool), y Qt no admite destruir objetos desde otro hilo: la aplicación se caía.
    """

    done = Signal(int, object)
    failed = Signal(int, object)
    progress = Signal(int, object)

    def __init__(self) -> None:
        super().__init__()
        self.pending: dict[int, _Callbacks] = {}
        # Receptor = este QObject: los slots se ejecutan en su hilo (el de la interfaz).
        self.done.connect(self._done)
        self.failed.connect(self._failed)
        self.progress.connect(self._progressed)

    @Slot(int, object)
    def _done(self, task_id: int, result: Any) -> None:
        callbacks = self.pending.pop(task_id, None)
        if callbacks is not None and callbacks.on_done is not None:
            callbacks.on_done(result)

    @Slot(int, object)
    def _failed(self, task_id: int, exc: Exception) -> None:
        callbacks = self.pending.pop(task_id, None)
        if callbacks is not None and callbacks.on_error is not None:
            callbacks.on_error(exc)
        else:
            log.error("Tarea fallida: %s", exc)

    @Slot(int, object)
    def _progressed(self, task_id: int, value: Any) -> None:
        callbacks = self.pending.get(task_id)
        if callbacks is not None and callbacks.on_progress is not None:
            callbacks.on_progress(value)


@dataclass(slots=True)
class _Callbacks:
    on_done: Callable[[Any], object] | None
    on_error: Callable[[Exception], object] | None
    on_progress: Callable[[Any], object] | None


class _Task(QRunnable):
    def __init__(self, fn: Callable[..., Any], hub: _Hub, task_id: int, progress: bool) -> None:
        super().__init__()
        self._fn = fn
        self._hub = hub
        self._id = task_id
        self._progress = progress

    def run(self) -> None:
        hub, task_id = self._hub, self._id
        try:
            args = ((lambda value: hub.progress.emit(task_id, value)),) if self._progress else ()
            result = self._fn(*args)
        except Exception as exc:
            log.debug("Tarea fallida", exc_info=True)
            hub.failed.emit(task_id, exc)
        else:
            hub.done.emit(task_id, result)


class Runner:
    """Lanza funciones en el pool y entrega el resultado en el hilo de la interfaz.

    Se crea en el hilo de la interfaz.
    """

    def __init__(self, pool: QThreadPool | None = None, *, threads: int | None = None) -> None:
        if pool is None:
            pool = QThreadPool()
            if threads is not None:
                pool.setMaxThreadCount(threads)
        self.pool = pool
        self._hub = _Hub()
        self._ids = itertools.count()

    def submit(
        self,
        fn: Callable[..., Any],
        *,
        on_done: Callable[[Any], object] | None = None,
        on_error: Callable[[Exception], object] | None = None,
        on_progress: Callable[[Any], object] | None = None,
    ) -> None:
        """``fn()`` en segundo plano; con ``on_progress``, se llama ``fn(progress)``."""
        task_id = next(self._ids)
        self._hub.pending[task_id] = _Callbacks(on_done, on_error, on_progress)
        self.pool.start(_Task(fn, self._hub, task_id, on_progress is not None))

    @property
    def busy(self) -> int:
        return len(self._hub.pending)

    def close(self, msecs: int = 5000) -> None:
        """Espera a las tareas en curso y descarta sus resultados."""
        self.pool.waitForDone(msecs)
        self._hub.pending.clear()
