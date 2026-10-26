"""Vigilancia de las carpetas de descargas.

Los eventos de inotify (vía watchdog) solo avisan de que algo cambió; la decisión se
toma en el hilo principal tras comprobar que la descarga terminó. Como inotify puede
perder eventos (cola llena, servicio parado), cada ``reconcile_seconds`` se relee la
carpeta entera: la cola deduplica por huella, así que ver dos veces un archivo no crea
dos sugerencias.

Solo se vigilan los archivos directamente dentro de cada carpeta: lo que hay en
subcarpetas (un zip extraído, un proyecto clonado) ya está organizado por alguien.

Las carpetas pueden estar en discos externos de cualquier sistema de archivos. Cada
``rewatch_seconds`` se comprueba si cada carpeta está disponible: al conectar el disco
se empieza a vigilar y se relee entera; al desconectarlo se deja de vigilar, y sus
sugerencias pendientes se conservan. En sistemas de red (NFS, SMB, sshfs) inotify no ve
los cambios hechos desde otro equipo, así que se sondean.
"""

from __future__ import annotations

import contextlib
import logging
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from watchdog.events import (
    FileClosedEvent,
    FileCreatedEvent,
    FileModifiedEvent,
    FileMovedEvent,
    FileSystemEvent,
    FileSystemEventHandler,
)
from watchdog.observers import Observer
from watchdog.observers.api import BaseObserver, ObservedWatch
from watchdog.observers.polling import PollingObserver

from airgnizer.actions import ActionError
from airgnizer.core.config import WatchSettings
from airgnizer.downloads.completion import Arrival, StabilityTracker
from airgnizer.downloads.queue import SuggestionQueue
from airgnizer.features import FileFeatures, extract_features
from airgnizer.learning.model import Prediction
from airgnizer.scanner.mounts import filesystem_of
from airgnizer.storage.models import DownloadSuggestion, SuggestionState

log = logging.getLogger(__name__)

# Solo eventos que cambian contenido o nombre. Las aperturas y lecturas (IN_OPEN,
# IN_CLOSE_NOWRITE) se ignoran: las provocan el propio análisis del archivo, Baloo o un
# visor, y volverían a meter el archivo en la cola de espera una y otra vez.
EVENT_FILTER: list[type[FileSystemEvent]] = [
    FileCreatedEvent,
    FileModifiedEvent,
    FileMovedEvent,
    FileClosedEvent,
]

# Segundos entre lecturas de una carpeta sondeada (sistemas de archivos de red).
NETWORK_POLL_SECONDS = 5.0

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
        self._tracker = StabilityTracker(settings.stable_seconds, is_known=queue.knows)
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
            self._reconcile_dir(directory, now)
        expired = self._queue.expire_missing()
        if expired:
            log.info("%d sugerencias caducadas (el archivo ya no está)", expired)

    def _reconcile_dir(self, directory: Path, now: float | None) -> None:
        if not directory.is_dir():
            return  # disco desconectado: nada que leer
        try:
            entries = list(directory.iterdir())
        except OSError as exc:
            log.warning("No se puede leer %s: %s", directory, exc)
            return
        for entry in entries:
            self.notice(entry, now)

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

    def start(self, *, rewatch_seconds: float = 5.0) -> None:
        """Empieza a vigilar; después hay que llamar a ``tick`` periódicamente y a ``close``."""
        self._watches = _Watches(self, PollingObserver(timeout=NETWORK_POLL_SECONDS))
        self._rewatch_seconds = rewatch_seconds
        self._next_rewatch = self._next_reconcile = 0.0

    def tick(self, now: float | None = None) -> list[Outcome]:
        """Un paso del bucle: vigilancias, reconciliación si toca y descargas terminadas."""
        now = time.monotonic() if now is None else now
        if now >= self._next_rewatch:
            for directory in self._watches.sync():
                self._reconcile_dir(directory, now)  # recién conectada
            self._next_rewatch = now + self._rewatch_seconds
        if now >= self._next_reconcile:
            self.reconcile(now)
            self._next_reconcile = now + self._settings.reconcile_seconds
        return self.process()

    def close(self) -> None:
        self._watches.close()

    def run(
        self,
        stop: threading.Event,
        on_outcome: Callable[[Outcome], None] = lambda _o: None,
        *,
        poll_seconds: float = 0.5,
        rewatch_seconds: float = 5.0,
    ) -> None:
        """Vigila hasta que se active ``stop``."""
        self.start(rewatch_seconds=rewatch_seconds)
        try:
            while not stop.is_set():
                for outcome in self.tick():
                    on_outcome(outcome)
                stop.wait(poll_seconds)
        finally:
            self.close()


class _Watches:
    """Vigilancias activas, una por carpeta disponible."""

    def __init__(self, monitor: DownloadMonitor, polling: BaseObserver) -> None:
        self._monitor = monitor
        self._native: BaseObserver = Observer()
        self._polling = polling
        self._native.start()
        self._polling.start()
        # carpeta → (observador, vigilancia, identidad (st_dev, st_ino) al empezar)
        self._active: dict[Path, tuple[BaseObserver, ObservedWatch, tuple[int, int]]] = {}

    def sync(self) -> list[Path]:
        """Ajusta las vigilancias a las carpetas disponibles; devuelve las recién añadidas."""
        added: list[Path] = []
        for directory in self._monitor.dirs:
            try:
                st = directory.stat()
                identity: tuple[int, int] | None = (st.st_dev, st.st_ino)
            except OSError:
                identity = None
            current = self._active.get(directory)
            if current is not None and current[2] == identity:
                continue
            if current is not None:
                self._drop(directory)
                log.info("Carpeta vigilada no disponible (¿disco desconectado?): %s", directory)
            if identity is not None and self._add(directory, identity):
                added.append(directory)
        return added

    def _add(self, directory: Path, identity: tuple[int, int]) -> bool:
        mount = filesystem_of(directory)
        network = mount is not None and mount.is_network
        observer = self._polling if network else self._native
        try:
            watch = self._schedule(observer, directory)
        except OSError as exc:
            if network:
                log.warning("No se puede vigilar %s: %s", directory, exc)
                return False
            # Sin inotify (límite de vigilancias, FUSE sin soporte): sondeo.
            log.info("inotify no disponible en %s (%s): se sondea", directory, exc)
            observer = self._polling
            watch = self._schedule(observer, directory)
        self._active[directory] = (observer, watch, identity)
        fstype = mount.fstype if mount else "?"
        log.info("Vigilando %s (%s, %s)", directory, fstype, "sondeo" if network else "inotify")
        return True

    def _schedule(self, observer: BaseObserver, directory: Path) -> ObservedWatch:
        handler = _Handler(self._monitor)
        return observer.schedule(
            handler, str(directory), recursive=False, event_filter=EVENT_FILTER
        )

    def _drop(self, directory: Path) -> None:
        observer, watch, _ = self._active.pop(directory)
        # El emisor puede haberse detenido ya al desmontarse el disco.
        with contextlib.suppress(KeyError, OSError):
            observer.unschedule(watch)

    def close(self) -> None:
        for observer in (self._native, self._polling):
            observer.stop()
        for observer in (self._native, self._polling):
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
