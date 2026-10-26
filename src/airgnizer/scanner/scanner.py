"""Indexado incremental de árboles de directorios.

El escáner **solo lee**: nunca modifica archivos del usuario. Un archivo se
considera modificado si cambia cualquiera de tamaño, ``mtime``, ``ctime``,
inode o dispositivo; en ese caso se invalidan sus hashes.
"""

from __future__ import annotations

import fnmatch
import logging
import os
import stat
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import ColumnElement, delete, func, insert, select, update
from sqlalchemy.orm import Session

from airgnizer.core.config import ScanSettings
from airgnizer.scanner.mounts import mount_points
from airgnizer.storage import Database
from airgnizer.storage.models import Directory, File, ScanError, ScanRun, utcnow

log = logging.getLogger(__name__)

COMMIT_EVERY_DIRS = 200
PROGRESS_EVERY_FILES = 500


@dataclass(slots=True)
class ScanStats:
    scan_id: int = 0
    root: Path = Path()
    dirs_seen: int = 0
    files_seen: int = 0
    files_new: int = 0
    files_changed: int = 0
    files_removed: int = 0
    skipped: int = 0
    errors: int = 0
    cancelled: bool = False
    current_dir: Path | None = field(default=None, repr=False)


ProgressCallback = Callable[[ScanStats], None]


def is_hidden(name: str) -> bool:
    """Oculto según la convención Unix (empieza por «.»), salvo los puntos suspensivos.

    «... Baby One More Time.mp3» viene de un título que empieza por «...», no es un
    archivo de configuración. Un nombre formado solo por puntos sigue siendo oculto.
    """
    rest = name.lstrip(".")
    leading_dots = len(name) - len(rest)
    if leading_dots == 0:
        return False
    return leading_dots < 3 or not rest


def _signature(st: os.stat_result) -> tuple[int, int, int, int, int]:
    return (st.st_size, st.st_mtime_ns, st.st_ctime_ns, st.st_ino, st.st_dev)


def _subtree(directory: Path) -> ColumnElement[bool]:
    prefix = str(directory).rstrip("/") + "/"
    return (Directory.path == str(directory)) | Directory.path.startswith(prefix, autoescape=True)


# Columnas derivadas del contenido que se anulan cuando el archivo cambia.
_INVALIDATED: dict[str, Any] = {
    "partial_hash": None,
    "content_hash": None,
    "perceptual_hash": None,
    "image_width": None,
    "image_height": None,
    "mime": None,
    "features": None,
    "features_version": None,
}


class Scanner:
    def __init__(
        self,
        db: Database,
        settings: ScanSettings,
        *,
        mounts: Iterable[Path] | None = None,
    ) -> None:
        self._db = db
        self.settings = settings
        self._excluded_names = frozenset(settings.excluded_names)
        self._mounts = frozenset(mounts) if mounts is not None else mount_points()

    # --- filtros -------------------------------------------------------------

    def is_excluded(self, name: str) -> bool:
        if not self.settings.include_hidden and is_hidden(name):
            return True
        if name in self._excluded_names:
            return True
        return any(fnmatch.fnmatchcase(name, pat) for pat in self.settings.excluded_globs)

    def crosses_mount(self, path: Path) -> bool:
        return not self.settings.cross_mounts and path in self._mounts

    # --- escaneo -------------------------------------------------------------

    def scan(
        self,
        root: Path,
        *,
        cancel: threading.Event | None = None,
        progress: ProgressCallback | None = None,
    ) -> ScanStats:
        root = root.expanduser().resolve()
        if not root.is_dir():
            raise NotADirectoryError(root)

        with self._db.new_session() as session:
            run = ScanRun(root=str(root))
            session.add(run)
            session.commit()
            stats = ScanStats(scan_id=run.id, root=root)
            job = _ScanJob(self, session, stats, cancel, progress)
            try:
                job.walk()
                if not stats.cancelled:
                    # Solo con un recorrido completo se puede afirmar que algo desapareció.
                    stats.files_removed += job.remove_unseen()
            finally:
                run.finished_at = utcnow()
                run.cancelled = stats.cancelled
                run.files_seen = stats.files_seen
                run.files_changed = stats.files_new + stats.files_changed
                run.files_removed = stats.files_removed
                run.errors = stats.errors
                session.commit()

        log.info("Escaneo de %s terminado: %s", root, stats)
        return stats


@dataclass(slots=True)
class _KnownFile:
    file_id: int
    signature: tuple[int, int, int, int, int]


@dataclass(slots=True)
class _DirBatch:
    """Cambios de una carpeta, que se escriben juntos al terminar de listarla."""

    dir_id: int
    known: dict[str, _KnownFile]
    new_rows: list[dict[str, Any]] = field(default_factory=list)
    changed_rows: list[dict[str, Any]] = field(default_factory=list)
    indexed: set[str] = field(default_factory=set)


class _ScanJob:
    """Estado de una ejecución de escaneo sobre una raíz.

    Usa SQLAlchemy Core en lugar de objetos ORM: en un escaneo incremental casi nada
    cambia, y solo se escribe lo que cambió (archivos nuevos, modificados o desaparecidos).
    Los archivos desaparecidos se detectan carpeta a carpeta, comparando el listado con
    el índice; las carpetas desaparecidas, al final, y solo tras un recorrido completo.
    """

    def __init__(
        self,
        scanner: Scanner,
        session: Session,
        stats: ScanStats,
        cancel: threading.Event | None,
        progress: ProgressCallback | None,
    ) -> None:
        self.scanner = scanner
        self.session = session
        self.stats = stats
        self.root = str(stats.root)
        self.cancel = cancel
        self.progress = progress
        self.follow_symlinks = scanner.settings.follow_symlinks
        # Carpetas ya indexadas bajo la raíz (de esta raíz o de otra que la contenga).
        self.known_dirs: dict[str, tuple[int, int]] = {
            path: (dir_id, mtime)
            for dir_id, path, mtime in session.execute(
                select(Directory.id, Directory.path, Directory.mtime_ns).where(_subtree(stats.root))
            )
        }
        self.seen_dirs: list[int] = []

    def walk(self) -> None:
        # Recorrido iterativo (sin recursión) con detección de ciclos para symlinks.
        stack: list[Path] = [self.stats.root]
        visited: set[tuple[int, int]] = set()

        while stack:
            if self.cancel is not None and self.cancel.is_set():
                self.stats.cancelled = True
                break
            directory = stack.pop()
            try:
                dir_stat = directory.stat()
            except OSError as exc:
                self.record_error(directory, exc)
                continue
            key = (dir_stat.st_dev, dir_stat.st_ino)
            if key in visited:
                self.stats.skipped += 1
                continue
            visited.add(key)

            self.stats.current_dir = directory
            stack.extend(self.scan_directory(directory, dir_stat))
            self.stats.dirs_seen += 1
            if len(self.seen_dirs) >= COMMIT_EVERY_DIRS:
                self.flush_seen_dirs()
                self.session.commit()
        self.flush_seen_dirs()
        self.session.commit()

    def directory_id(self, directory: Path, dir_stat: os.stat_result) -> tuple[int, bool]:
        """Id de la carpeta en el índice (creándola si hace falta) y si es nueva."""
        known = self.known_dirs.get(str(directory))
        if known is None:
            dir_id = self.session.execute(
                insert(Directory)
                .values(
                    path=str(directory),
                    root=self.root,
                    mtime_ns=dir_stat.st_mtime_ns,
                    last_seen_scan=self.stats.scan_id,
                )
                .returning(Directory.id)
            ).scalar_one()
            return dir_id, True
        dir_id, mtime = known
        if mtime != dir_stat.st_mtime_ns:
            self.session.execute(
                update(Directory)
                .where(Directory.id == dir_id)
                .values(mtime_ns=dir_stat.st_mtime_ns)
            )
        self.seen_dirs.append(dir_id)
        return dir_id, False

    def flush_seen_dirs(self) -> None:
        if self.seen_dirs:
            self.session.execute(
                update(Directory)
                .where(Directory.id.in_(self.seen_dirs))
                .values(last_seen_scan=self.stats.scan_id, root=self.root)
            )
            self.seen_dirs.clear()

    def scan_directory(self, directory: Path, dir_stat: os.stat_result) -> list[Path]:
        dir_id, is_new = self.directory_id(directory, dir_stat)
        subdirs: list[Path] = []

        try:
            entries = list(os.scandir(directory))
        except OSError as exc:
            self.record_error(directory, exc)
            # Sin listado no se puede saber qué desapareció: se conserva el subárbol.
            self.keep_subtree(directory)
            return subdirs

        batch = _DirBatch(dir_id, {} if is_new else self.load_known(dir_id))

        for entry in entries:
            if self.scanner.is_excluded(entry.name):
                self.stats.skipped += 1
                continue
            path = Path(entry.path)
            try:
                if entry.is_symlink() and not self.follow_symlinks:
                    self.stats.skipped += 1
                    continue
                st = entry.stat(follow_symlinks=self.follow_symlinks)
            except OSError as exc:
                self.record_error(path, exc)
                continue

            if stat.S_ISDIR(st.st_mode):
                if self.scanner.crosses_mount(path):
                    self.stats.skipped += 1
                else:
                    subdirs.append(path)
            elif stat.S_ISREG(st.st_mode):
                self.index_file(batch, entry.name, st)
            else:
                # sockets, FIFOs, dispositivos: no son archivos de usuario.
                self.stats.skipped += 1

        self.apply(batch)
        return subdirs

    def load_known(self, dir_id: int) -> dict[str, _KnownFile]:
        rows = self.session.execute(
            select(
                File.id, File.name, File.size, File.mtime_ns, File.ctime_ns, File.inode, File.device
            ).where(File.directory_id == dir_id)
        )
        return {
            name: _KnownFile(fid, (size, mtime, ctime, ino, dev))
            for fid, name, size, mtime, ctime, ino, dev in rows
        }

    def apply(self, batch: _DirBatch) -> None:
        if batch.new_rows:
            self.session.execute(insert(File), batch.new_rows)
        if batch.changed_rows:
            self.session.execute(update(File), batch.changed_rows)
        # Listado completo: lo indexado que ya no está (o ya no se indexa) se retira.
        gone = [k.file_id for name, k in batch.known.items() if name not in batch.indexed]
        if gone:
            self.session.execute(delete(File).where(File.id.in_(gone)))
            self.stats.files_removed += len(gone)

    def index_file(self, batch: _DirBatch, name: str, st: os.stat_result) -> None:
        stats = self.stats
        batch.indexed.add(name)
        known = batch.known.get(name)
        stats.files_seen += 1
        if self.progress is not None and stats.files_seen % PROGRESS_EVERY_FILES == 0:
            self.progress(stats)

        values = {
            "size": st.st_size,
            "mtime_ns": st.st_mtime_ns,
            "ctime_ns": st.st_ctime_ns,
            "inode": st.st_ino,
            "device": st.st_dev,
            "nlink": st.st_nlink,
            "last_seen_scan": stats.scan_id,
        }
        if known is None:
            batch.new_rows.append({"directory_id": batch.dir_id, "name": name, **values})
            stats.files_new += 1
        elif known.signature != _signature(st):
            # Invalidación conservadora: cualquier cambio anula hashes y metadatos derivados.
            batch.changed_rows.append({"id": known.file_id, **values, **_INVALIDATED})
            stats.files_changed += 1

    def keep_subtree(self, directory: Path) -> None:
        self.session.execute(
            update(Directory).where(_subtree(directory)).values(last_seen_scan=self.stats.scan_id)
        )

    def remove_unseen(self) -> int:
        """Retira las carpetas de la raíz que no aparecieron (con sus archivos, en cascada)."""
        unseen = (Directory.root == self.root) & (Directory.last_seen_scan < self.stats.scan_id)
        removed = (
            self.session.scalar(select(func.count(File.id)).join(Directory).where(unseen)) or 0
        )
        self.session.execute(delete(Directory).where(unseen))
        self.session.commit()
        return int(removed)

    def record_error(self, path: Path, exc: OSError) -> None:
        self.stats.errors += 1
        log.debug("Error al leer %s: %s", path, exc)
        self.session.add(
            ScanError(scan_id=self.stats.scan_id, path=str(path), errno=exc.errno, message=str(exc))
        )
