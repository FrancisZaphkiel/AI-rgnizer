"""Estado compartido por las vistas: contexto, backend, tareas y avisos de cambios."""

from __future__ import annotations

import logging

from PySide6.QtCore import QObject, QTimer, Signal, Slot

from organizador import overview
from organizador.app import AppContext
from organizador.backend import Backend
from organizador.gui.workers import Runner
from organizador.service.client import RemoteBackend

log = logging.getLogger(__name__)

LOCAL_POLL_MS = 5000


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
        self.runner = runner or Runner()
        self._last_count = -1
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

    @Slot()
    def _on_remote_event(self) -> None:
        self.notify_changed()

    def notify_changed(self) -> None:
        self.changed.emit()
        self.refresh_count()

    @Slot()
    def refresh_count(self) -> None:
        count = len(overview.pending_suggestions(self.ctx))
        if count != self._last_count:
            self._last_count = count
            self.pending_count.emit(count)

    def report(self, exc: Exception) -> None:
        log.warning("%s", exc)
        self.error.emit(str(exc))
