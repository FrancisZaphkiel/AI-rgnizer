"""Vigilancia de las carpetas de descargas.

Los eventos de inotify (vía watchdog) solo avisan de que algo cambió; la decisión se
toma en el hilo principal tras comprobar que la descarga terminó. Como inotify puede
perder eventos (cola llena, servicio parado), cada ``reconcile_seconds`` se relee la
carpeta entera: la cola deduplica por huella, así que ver dos veces un archivo no crea
dos sugerencias.

Solo se vigilan los archivos directamente dentro de cada carpeta: lo que hay en
subcarpetas (un zip extraído, un proyecto clonado) ya está organizado por alguien.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from organizador.actions import ActionError
from organizador.core.config import WatchSettings
from organizador.downloads.completion import Arrival, StabilityTracker
from organizador.downloads.queue import SuggestionQueue
from organizador.features import FileFeatures, extract_features
from organizador.learning.model import Prediction
from organizador.storage.models import DownloadSuggestion, SuggestionState

log = logging.getLogger(__name__)

# Solo estas fuentes dan una probabilidad calibrada (o una orden explícita del usuario).
AUTO_SOURCES = frozenset({"configuración", "regla", "modelo"})

Classifier = Callable[[FileFeatures], Prediction]
# Mueve un archivo a una carpeta y devuelve el id de la acción en el journal.
Mover = Callable[[Path, Path], str]


@dataclass(frozen=True, slots=True)
class Outcome:
    suggestion: DownloadSuggestion
    # Destino real si el modo automático movió el archivo.
    moved_to: Path | None = None


def auto_eligible(suggestion: DownloadSuggestion, settings: WatchSettings) -> bool:
    return (
        settings.mode == "auto"
        and suggestion.destination is not None
        and suggestion.source in AUTO_SOURCES
        and (suggestion.confidence or 0.0) >= settings.auto_threshold
    )


class DownloadMonitor:
    def __init__(
        self,
        dirs: Sequence[Path],
        settings: WatchSettings,
        queue: SuggestionQueue,
        classify: Classifier,
        mover: Mover | None = None,
    ) -> None:
        self.dirs = [d.expanduser().absolute() for d in dirs]
        self._settings = settings
        self._queue = queue
        self._classify = classify
        self._mover = mover
        self._tracker = StabilityTracker(settings.stable_seconds)
        self._lock = threading.Lock()

    # --- entrada (puede llamarse desde los hilos de watchdog) ------------------------

    def notice(self, path: Path, now: float | None = None) -> None:
        path = path.absolute()
        if path.parent not in self.dirs:
            return
        with self._lock:
            self._tracker.notice(path, time.monotonic() if now is None else now)

    def reconcile(self, now: float | None = None) -> None:
        """Relee las carpetas vigiladas y caduca las sugerencias de archivos que ya no están."""
        for directory in self.dirs:
            try:
                entries = list(directory.iterdir())
            except OSError as exc:
                log.warning("No se puede leer %s: %s", directory, exc)
                continue
            for entry in entries:
                self.notice(entry, now)
        expired = self._queue.expire_missing()
        if expired:
            log.info("%d sugerencias caducadas (el archivo ya no está)", expired)

    @property
    def waiting(self) -> int:
        with self._lock:
            return len(self._tracker)

    # --- decisión (hilo principal) ---------------------------------------------------

    def process(self, now: float | None = None) -> list[Outcome]:
        """Sugiere destino para las descargas terminadas y, en modo automático, las mueve."""
        with self._lock:
            ready = self._tracker.ready(time.monotonic() if now is None else now)
        return [o for a in ready if (o := self._handle(a)) is not None]

    def _handle(self, arrival: Arrival) -> Outcome | None:
        try:
            features = extract_features(arrival.path)
        except OSError as exc:
            log.debug("Ya no se puede leer %s: %s", arrival.path, exc)
            return None
        suggestion = self._queue.add(arrival, self._classify(features))
        if suggestion is None:
            return None  # ya sugerido (reinicio del servicio o reconciliación)
        if self._mover is None or not auto_eligible(suggestion, self._settings):
            return Outcome(suggestion)
        assert suggestion.destination is not None
        destination = Path(suggestion.destination)
        if not destination.is_dir():
            log.info("Destino inexistente %s: %s queda pendiente", destination, arrival.path)
            return Outcome(suggestion)
        try:
            action_id = self._mover(arrival.path, destination)
        except ActionError as exc:
            log.warning("No se pudo mover %s: %s", arrival.path, exc)
            return Outcome(suggestion)
        resolved = self._queue.resolve(
            suggestion.id, SuggestionState.AUTO, chosen=suggestion.destination, action_id=action_id
        )
        return Outcome(resolved, moved_to=destination)

    # --- bucle -----------------------------------------------------------------------

    def run(
        self,
        stop: threading.Event,
        on_outcome: Callable[[Outcome], None] = lambda _o: None,
        *,
        poll_seconds: float = 0.5,
    ) -> None:
        """Vigila hasta que se active ``stop``."""
        observer = Observer()
        for directory in self.dirs:
            if directory.is_dir():
                observer.schedule(_Handler(self), str(directory), recursive=False)
            else:
                log.warning("La carpeta vigilada no existe: %s", directory)
        observer.start()
        try:
            next_reconcile = 0.0
            while not stop.is_set():
                now = time.monotonic()
                if now >= next_reconcile:
                    self.reconcile(now)
                    next_reconcile = now + self._settings.reconcile_seconds
                for outcome in self.process():
                    on_outcome(outcome)
                stop.wait(poll_seconds)
        finally:
            observer.stop()
            observer.join()


class _Handler(FileSystemEventHandler):
    def __init__(self, monitor: DownloadMonitor) -> None:
        self._monitor = monitor

    def on_any_event(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        # Al renombrar «x.part» → «x» interesa el destino; al crear, el origen.
        for raw in (event.src_path, event.dest_path):
            if raw:
                self._monitor.notice(Path(str(raw)))
