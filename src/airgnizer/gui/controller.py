"""Estado compartido por las vistas: contexto, backend, tareas y avisos de cambios.

Para que la interfaz no se congele:

- las tareas van en tres grupos de hilos separados: lecturas (``runner``), operaciones
  sobre archivos (``ops``, de una en una y en orden) y trabajos largos (``jobs``), así
  una búsqueda de duplicados o un lote de 400 movimientos no deja sin hilos a las
  lecturas que refrescan la pantalla;
- los avisos de cambio se agrupan (``DEBOUNCE_MS``): aceptar 50 descargas son 50 señales
  del servicio pero un solo refresco;
- cada vista se recarga con un ``Refresher``: solo si está visible y sin consultas
  solapadas (las ocultas quedan marcadas y se recargan al mostrarse).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QObject, QTimer, Signal, Slot
from PySide6.QtWidgets import QWidget

from airgnizer import overview
from airgnizer.app import AppContext
from airgnizer.backend import Backend
from airgnizer.gui.workers import Runner
from airgnizer.service.client import RemoteBackend

log = logging.getLogger(__name__)

LOCAL_POLL_MS = 5000
DEBOUNCE_MS = 250


class Refresher:
    """Recarga de una vista en segundo plano: solo visible, sin solaparse, la última gana."""

    def __init__(
        self,
        widget: QWidget,
        runner: Runner,
        load: Callable[[], Any],
        show: Callable[[Any], object],
        on_error: Callable[[Exception], object],
    ) -> None:
        self._widget = widget
        self._runner = runner
        self._load = load
        self._show = show
        self._on_error = on_error
        self._busy = False
        self._again = False
        self.stale = True

    def request(self) -> None:
        if not self._widget.isVisible():
            self.stale = True  # se recarga al mostrarse
            return
        if self._busy:
            self._again = True  # una sola recarga más al terminar la actual
            return
        self._busy, self.stale = True, False
        self._runner.submit(self._load, on_done=self._done, on_error=self._failed)

    def shown(self) -> None:
        if self.stale:
            self.request()

    def _done(self, result: Any) -> None:
        self._busy = False
        if self._again:
            self._again = False
            self.request()  # el resultado ya está viejo: se pide otro y no se pinta este
            return
        self._show(result)

    def _failed(self, exc: Exception) -> None:
        self._busy = False
        self._on_error(exc)


class Controller(QObject):
    # Algo cambió en las sugerencias o en el historial: las vistas se refrescan.
    changed = Signal()
    pending_count = Signal(int)
    error = Signal(str)
    # Señal interna: el hilo de D-Bus la emite y se atiende en el de la interfaz.
    _remote_event = Signal()

    def __init__(self, ctx: AppContext, backend: Backend, runner: Runner | None = None) -> None:
        super().__init__()
        self.ctx = ctx
        self.backend = backend
        self.runner = runner or Runner(threads=2)
        self.ops = Runner(threads=1)
        self.jobs = Runner(threads=1)
        self._last_count = -1
        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(DEBOUNCE_MS)
        self._debounce.timeout.connect(self._emit_changed)
        self._count = Refresher(
            _always_visible,
            self.runner,
            lambda: len(overview.pending_suggestions(ctx)),
            self._show_count,
            lambda exc: log.warning("No se pudo contar las pendientes: %s", exc),
        )
        self._remote_event.connect(self._on_remote_event)
        if isinstance(backend, RemoteBackend):
            for name in ("SuggestionAdded", "SuggestionResolved"):
                backend.client.on(name, lambda *_: self._remote_event.emit())
        else:
            # Sin servicio no hay señales: se consulta la base de vez en cuando.
            self._timer = QTimer(self)
            self._timer.timeout.connect(self.refresh_count)
            self._timer.start(LOCAL_POLL_MS)

    @property
    def remote(self) -> bool:
        return self.backend.remote

    @property
    def busy(self) -> bool:
        """Hay tareas o un refresco pendiente (lo usan las pruebas para esperar)."""
        return self._debounce.isActive() or any(r.busy for r in (self.runner, self.ops, self.jobs))

    def close(self) -> None:
        """Para los temporizadores y espera a las tareas (al salir de la aplicación)."""
        self._debounce.stop()
        if hasattr(self, "_timer"):
            self._timer.stop()
        for runner in (self.runner, self.ops, self.jobs):
            runner.close()

    @Slot()
    def _on_remote_event(self) -> None:
        self.notify_changed()

    def notify_changed(self) -> None:
        """Pide un refresco; los avisos seguidos se agrupan en uno."""
        self._debounce.start()

    @Slot()
    def _emit_changed(self) -> None:
        self.changed.emit()
        self.refresh_count()

    @Slot()
    def refresh_count(self) -> None:
        self._count.request()

    def _show_count(self, count: int) -> None:
        if count != self._last_count:
            self._last_count = count
            self.pending_count.emit(count)

    def refresher(
        self, widget: QWidget, load: Callable[[], Any], show: Callable[[Any], object]
    ) -> Refresher:
        return Refresher(widget, self.runner, load, show, self.report)

    def report(self, exc: Exception) -> None:
        log.warning("%s", exc)
        self.error.emit(str(exc))


class _AlwaysVisible:
    """El contador de la bandeja se actualiza aunque la ventana esté oculta."""

    def isVisible(self) -> bool:  # noqa: N802 - imita a QWidget
        return True


_always_visible: Any = _AlwaysVisible()
