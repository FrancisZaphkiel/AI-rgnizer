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
from typing import Any, cast

from sqlalchemy import CursorResult, delete, select, update
from sqlalchemy.orm import Session

from organizador.core.config import ScanSettings
from organizador.scanner.mounts import mount_points
from organizador.storage import Database
from organizador.storage.models import Directory, File, ScanError, ScanRun, utcnow

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


def _file_signature(f: File) -> tuple[int, int, int, int, int]:
    return (f.size, f.mtime_ns, f.ctime_ns, f.inode, f.device)


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
                    stats.files_removed = job.remove_unseen()
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


class _ScanJob:
    """Estado de una ejecución de escaneo sobre una raíz."""

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

    def walk(self) -> None:
        # Recorrido iterativo (sin recursión) con detección de ciclos para symlinks.
        stack: list[Path] = [self.stats.root]
        visited: set[tuple[int, int]] = set()
        pending_dirs = 0

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

            pending_dirs += 1
            if pending_dirs >= COMMIT_EVERY_DIRS:
                self.session.commit()
                pending_dirs = 0
        self.session.commit()

    def scan_directory(self, directory: Path, dir_stat: os.stat_result) -> list[Path]:
        dir_row = self.session.scalar(select(Directory).where(Directory.path == str(directory)))
        if dir_row is None:
            dir_row = Directory(path=str(directory), root=self.root)
            self.session.add(dir_row)
        dir_row.root = self.root
        dir_row.mtime_ns = dir_stat.st_mtime_ns
        dir_row.last_seen_scan = self.stats.scan_id
        self.session.flush()

        known = {f.name: f for f in dir_row.files}
        subdirs: list[Path] = []

        try:
            entries = list(os.scandir(directory))
        except OSError as exc:
            self.record_error(directory, exc)
            # Sin listado no se puede saber qué desapareció: se conserva el subárbol.
            self.keep_subtree(directory)
            return subdirs

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
                self.index_file(dir_row, known.get(entry.name), entry.name, st)
            else:
                # sockets, FIFOs, dispositivos: no son archivos de usuario.
                self.stats.skipped += 1
        return subdirs

    def index_file(
        self, dir_row: Directory, row: File | None, name: str, st: os.stat_result
    ) -> None:
        stats = self.stats
        stats.files_seen += 1
        if self.progress is not None and stats.files_seen % PROGRESS_EVERY_FILES == 0:
            self.progress(stats)

        if row is None:
            self.session.add(
                File(
                    directory=dir_row,
                    name=name,
                    size=st.st_size,
                    mtime_ns=st.st_mtime_ns,
                    ctime_ns=st.st_ctime_ns,
                    inode=st.st_ino,
                    device=st.st_dev,
                    nlink=st.st_nlink,
                    last_seen_scan=stats.scan_id,
                )
            )
            stats.files_new += 1
            return

        row.last_seen_scan = stats.scan_id
        if _file_signature(row) != _signature(st):
            row.size = st.st_size
            row.mtime_ns = st.st_mtime_ns
            row.ctime_ns = st.st_ctime_ns
            row.inode = st.st_ino
            row.device = st.st_dev
            row.nlink = st.st_nlink
            # Invalidación conservadora: cualquier cambio anula los hashes.
            row.partial_hash = None
            row.content_hash = None
            row.perceptual_hash = None
            row.image_width = None
            row.image_height = None
            row.mime = None
            stats.files_changed += 1

    def keep_subtree(self, directory: Path) -> None:
        prefix = str(directory).rstrip("/") + "/"
        subtree = (Directory.path == str(directory)) | Directory.path.startswith(
            prefix, autoescape=True
        )
        scan_id = self.stats.scan_id
        self.session.execute(update(Directory).where(subtree).values(last_seen_scan=scan_id))
        self.session.execute(
            update(File)
            .where(File.directory_id.in_(select(Directory.id).where(subtree)))
            .values(last_seen_scan=scan_id)
        )

    def remove_unseen(self) -> int:
        scan_id = self.stats.scan_id
        root_dirs = select(Directory.id).where(Directory.root == self.root)
        result = cast(
            "CursorResult[Any]",
            self.session.execute(
                delete(File).where(File.directory_id.in_(root_dirs), File.last_seen_scan < scan_id)
            ),
        )
        self.session.execute(
            delete(Directory).where(Directory.root == self.root, Directory.last_seen_scan < scan_id)
        )
        self.session.commit()
        return int(result.rowcount or 0)

    def record_error(self, path: Path, exc: OSError) -> None:
        self.stats.errors += 1
        log.debug("Error al leer %s: %s", path, exc)
        self.session.add(
            ScanError(scan_id=self.stats.scan_id, path=str(path), errno=exc.errno, message=str(exc))
        )
