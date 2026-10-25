"""Búsqueda de duplicados exactos sobre el índice del escáner.

Pipeline por etapas, de lo más barato a lo más caro:

1. agrupar por tamaño (consulta al índice, sin leer archivos);
2. hash parcial xxh3 del principio y el final (**solo filtro**);
3. hash completo BLAKE3: la única confirmación de que dos archivos son iguales.

Los hashes se guardan en el índice y se reutilizan mientras la firma del archivo
(tamaño, ``mtime``, ``ctime``, inode, dispositivo) no cambie. Antes de usar un hash,
guardado o recién calculado, se comprueba que la firma en disco coincide con la del
índice; si no, el archivo se descarta en esta búsqueda (hay que volver a escanear).

Los hardlinks (mismo inode) son un único archivo físico: se leen una sola vez, nunca
se proponen para la papelera si comparten inode con la copia conservada y solo cuentan
como espacio recuperable si se eliminan todos sus enlaces.
"""

from __future__ import annotations

import logging
import threading
from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Literal

from sqlalchemy import ColumnElement, func, or_, select, update
from sqlalchemy.orm import Session

from organizador.core.config import DuplicateSettings
from organizador.duplicates import hashing
from organizador.duplicates.birthtime import birth_time_ns
from organizador.duplicates.keep import KeepPolicy
from organizador.duplicates.space import disk_usage
from organizador.storage import Database
from organizador.storage.models import Directory, File

log = logging.getLogger(__name__)

COMMIT_EVERY_FILES = 200

InodeKey = tuple[int, int]  # (dispositivo, inode)
HashKind = Literal["partial", "full"]


@dataclass(frozen=True, slots=True)
class DuplicateFile:
    path: Path
    size: int
    mtime_ns: int
    device: int
    inode: int
    nlink: int
    # Fecha de creación (statx); None si el sistema de archivos no la guarda.
    birth_ns: int | None = None
    # Bytes que ocupa el archivo sin contar extents compartidos (reflinks, instantáneas).
    exclusive_bytes: int = 0

    @property
    def inode_key(self) -> InodeKey:
        return (self.device, self.inode)


@dataclass(frozen=True, slots=True)
class DuplicateGroup:
    """Archivos con el mismo contenido (BLAKE3) y al menos dos inodes distintos."""

    content_hash: str
    size: int
    files: tuple[DuplicateFile, ...]
    keep: DuplicateFile
    keep_reason: str

    def with_keep(self, keep: DuplicateFile) -> DuplicateGroup:
        """La elección del usuario prevalece sobre la recomendación."""
        if keep not in self.files:
            raise ValueError(f"{keep.path} no pertenece al grupo")
        return replace(self, keep=keep, keep_reason="elegida por el usuario")

    @property
    def hardlinks_of_keep(self) -> tuple[DuplicateFile, ...]:
        """Otros nombres del mismo inode conservado: no se proponen para eliminar."""
        return tuple(
            f for f in self.files if f is not self.keep and f.inode_key == self.keep.inode_key
        )

    @property
    def redundant(self) -> tuple[DuplicateFile, ...]:
        """Copias propuestas para la papelera."""
        return tuple(f for f in self.files if f.inode_key != self.keep.inode_key)

    @property
    def recoverable_bytes(self) -> int:
        """Espacio que se libera al eliminar las copias propuestas (estimación conservadora)."""
        links: dict[InodeKey, list[DuplicateFile]] = defaultdict(list)
        for f in self.redundant:
            links[f.inode_key].append(f)
        total = 0
        for same_inode in links.values():
            # Si quedan enlaces fuera del grupo, el inode no se libera.
            if same_inode[0].nlink <= len(same_inode):
                total += same_inode[0].exclusive_bytes
        return total


@dataclass(slots=True)
class DuplicateStats:
    candidates: int = 0
    partial_hashed: int = 0
    full_hashed: int = 0
    bytes_hashed: int = 0
    # Archivos cuyo estado en disco ya no coincide con el índice.
    stale: int = 0
    errors: int = 0
    cancelled: bool = False


@dataclass(slots=True)
class DuplicateReport:
    groups: list[DuplicateGroup] = field(default_factory=list)
    stats: DuplicateStats = field(default_factory=DuplicateStats)

    @property
    def recoverable_bytes(self) -> int:
        return sum(g.recoverable_bytes for g in self.groups)


ProgressCallback = Callable[[DuplicateStats], None]


@dataclass(slots=True)
class _Candidate:
    """Fila del índice que comparte tamaño con otras (estado mutable durante la búsqueda)."""

    file_id: int
    path: Path
    size: int
    mtime_ns: int
    ctime_ns: int
    device: int
    inode: int
    partial_hash: str | None
    content_hash: str | None
    nlink: int = 1

    @property
    def inode_key(self) -> InodeKey:
        return (self.device, self.inode)

    @property
    def signature(self) -> hashing.FileSignature:
        return (self.size, self.mtime_ns, self.ctime_ns, self.inode, self.device)

    def get_hash(self, kind: HashKind) -> str | None:
        return self.partial_hash if kind == "partial" else self.content_hash

    def set_hash(self, kind: HashKind, digest: str) -> None:
        if kind == "partial":
            self.partial_hash = digest
        else:
            self.content_hash = digest


def _birth_or_none(path: Path) -> int | None:
    try:
        return birth_time_ns(path)
    except OSError as exc:
        log.debug("No se pudo leer la fecha de creación de %s: %s", path, exc)
        return None


def _hash_of(links: Sequence[_Candidate], kind: HashKind) -> str:
    digest = links[0].get_hash(kind)
    if digest is None:  # pragma: no cover - hash_stage solo devuelve inodes con hash
        raise AssertionError(f"Falta el hash {kind} de {links[0].path}")
    return digest


def _distinct_inodes(group: Iterable[_Candidate]) -> int:
    return len({c.inode_key for c in group})


def _under_roots(roots: Sequence[Path]) -> ColumnElement[bool]:
    clauses: list[ColumnElement[bool]] = []
    for root in roots:
        prefix = str(root).rstrip("/") + "/"
        clauses.append(Directory.path == str(root))
        clauses.append(Directory.path.startswith(prefix, autoescape=True))
    return or_(*clauses)


class DuplicateFinder:
    def __init__(self, db: Database, settings: DuplicateSettings, policy: KeepPolicy) -> None:
        self._db = db
        self._settings = settings
        self._policy = policy

    def find(
        self,
        roots: Sequence[Path],
        *,
        cancel: threading.Event | None = None,
        progress: ProgressCallback | None = None,
    ) -> DuplicateReport:
        """Busca duplicados entre los archivos indexados bajo ``roots`` (solo lectura)."""
        resolved = [r.expanduser().resolve() for r in roots]
        report = DuplicateReport()
        with self._db.new_session() as session:
            job = _FindJob(self._settings, session, report.stats, cancel, progress)
            confirmed = job.run(resolved)
            session.commit()
        if not report.stats.cancelled:
            report.groups = [self._build_group(h, members) for h, members in confirmed.items()]
            report.groups.sort(key=lambda g: (-g.recoverable_bytes, -g.size, g.content_hash))
        log.info("Búsqueda de duplicados en %s: %s", resolved, report.stats)
        return report

    def _build_group(self, content_hash: str, members: list[_Candidate]) -> DuplicateGroup:
        usage: dict[InodeKey, int] = {}
        files: list[DuplicateFile] = []
        for c in sorted(members, key=lambda m: str(m.path)):
            if c.inode_key not in usage:
                try:
                    usage[c.inode_key] = disk_usage(c.path).exclusive
                except OSError as exc:
                    log.debug("No se pudo medir el uso en disco de %s: %s", c.path, exc)
                    usage[c.inode_key] = 0  # ante la duda, no se promete espacio
            files.append(
                DuplicateFile(
                    path=c.path,
                    size=c.size,
                    mtime_ns=c.mtime_ns,
                    device=c.device,
                    inode=c.inode,
                    nlink=c.nlink,
                    birth_ns=_birth_or_none(c.path),
                    exclusive_bytes=usage[c.inode_key],
                )
            )
        choice = self._policy.choose(files)
        return DuplicateGroup(
            content_hash=content_hash,
            size=members[0].size,
            files=tuple(files),
            keep=choice.keep,
            keep_reason=choice.reason,
        )


class _FindJob:
    def __init__(
        self,
        settings: DuplicateSettings,
        session: Session,
        stats: DuplicateStats,
        cancel: threading.Event | None,
        progress: ProgressCallback | None,
    ) -> None:
        self.settings = settings
        self.session = session
        self.stats = stats
        self.cancel = cancel
        self.progress = progress
        self._pending_writes = 0

    def run(self, roots: Sequence[Path]) -> dict[str, list[_Candidate]]:
        by_size = self.load_candidates(roots)
        self.stats.candidates = sum(len(g) for g in by_size.values())

        by_partial: dict[tuple[int, str], list[_Candidate]] = defaultdict(list)
        to_full: list[list[_Candidate]] = []
        for size, group in sorted(by_size.items(), reverse=True):
            if not hashing.needs_partial(size, self.settings.partial_bytes):
                to_full.append(group)
                continue
            for inode_group in self.hash_stage(group, "partial"):
                by_partial[(size, _hash_of(inode_group, "partial"))].extend(inode_group)
            if self.cancelled():
                return {}
        to_full.extend(g for g in by_partial.values() if _distinct_inodes(g) >= 2)

        by_content: dict[str, list[_Candidate]] = defaultdict(list)
        for group in to_full:
            for inode_group in self.hash_stage(group, "full"):
                by_content[_hash_of(inode_group, "full")].extend(inode_group)
            if self.cancelled():
                return {}
        return {h: g for h, g in by_content.items() if _distinct_inodes(g) >= 2}

    def cancelled(self) -> bool:
        if self.cancel is not None and self.cancel.is_set():
            self.stats.cancelled = True
        return self.stats.cancelled

    # --- etapa 1: tamaño -------------------------------------------------------

    def load_candidates(self, roots: Sequence[Path]) -> dict[int, list[_Candidate]]:
        in_roots = _under_roots(roots)
        repeated_sizes = (
            select(File.size)
            .join(Directory)
            .where(in_roots, File.size >= max(1, self.settings.min_size))
            .group_by(File.size)
            .having(func.count() > 1)
        )
        rows = self.session.execute(
            select(
                File.id,
                Directory.path,
                File.name,
                File.size,
                File.mtime_ns,
                File.ctime_ns,
                File.device,
                File.inode,
                File.partial_hash,
                File.content_hash,
            )
            .join(Directory)
            .where(in_roots, File.size.in_(repeated_sizes))
        )
        by_size: dict[int, list[_Candidate]] = defaultdict(list)
        for file_id, dir_path, name, size, mtime, ctime, dev, ino, partial, content in rows:
            by_size[size].append(
                _Candidate(
                    file_id, Path(dir_path) / name, size, mtime, ctime, dev, ino, partial, content
                )
            )
        # Varios nombres de un mismo inode no son duplicados entre sí.
        return {s: g for s, g in by_size.items() if _distinct_inodes(g) >= 2}

    # --- etapas 2 y 3: hashes ----------------------------------------------------

    def hash_stage(self, group: list[_Candidate], kind: HashKind) -> list[list[_Candidate]]:
        """Asegura el hash ``kind`` de cada candidato; devuelve los candidatos por inode.

        Los candidatos que cambiaron en disco o no se pueden leer se descartan.
        """
        by_inode: dict[InodeKey, list[_Candidate]] = defaultdict(list)
        for c in group:
            by_inode[c.inode_key].append(c)

        result: list[list[_Candidate]] = []
        for links in by_inode.values():
            if self.cancelled():
                break
            alive = [c for c in links if self.still_indexed(c)]
            if not alive:
                continue
            if self.ensure_hash(alive, kind):
                result.append(alive)
        return result

    def still_indexed(self, c: _Candidate) -> bool:
        try:
            st = c.path.lstat()
        except OSError:
            self.stats.stale += 1
            return False
        if hashing.signature(st) != c.signature:
            self.stats.stale += 1
            return False
        c.nlink = st.st_nlink
        return True

    def ensure_hash(self, links: list[_Candidate], kind: HashKind) -> bool:
        first = links[0]
        # Basta con que una de las filas del inode tenga el hash guardado.
        cached = next((h for c in links if (h := c.get_hash(kind)) is not None), None)
        if cached is not None:
            for c in links:
                c.set_hash(kind, cached)
            return True

        try:
            if kind == "partial":
                digest = hashing.partial_hash(first.path, first.size, self.settings.partial_bytes)
                self.stats.partial_hashed += 1
                self.stats.bytes_hashed += 2 * self.settings.partial_bytes
            else:
                digest = hashing.full_hash(first.path)
                self.stats.full_hashed += 1
                self.stats.bytes_hashed += first.size
            after = first.path.lstat()
        except OSError as exc:
            log.debug("No se pudo leer %s: %s", first.path, exc)
            self.stats.errors += 1
            return False
        if hashing.signature(after) != first.signature:
            # Cambió mientras se leía: el hash no corresponde a lo indexado.
            self.stats.stale += len(links)
            return False

        for c in links:
            c.set_hash(kind, digest)
        self.store(links, kind, digest)
        if self.progress is not None:
            self.progress(self.stats)
        return True

    def store(self, links: list[_Candidate], kind: HashKind, digest: str) -> None:
        column = File.partial_hash if kind == "partial" else File.content_hash
        first = links[0]
        # La condición sobre la firma evita pisar una fila que el escáner acaba de actualizar.
        self.session.execute(
            update(File)
            .where(
                File.id.in_([c.file_id for c in links]),
                File.size == first.size,
                File.mtime_ns == first.mtime_ns,
                File.ctime_ns == first.ctime_ns,
                File.inode == first.inode,
                File.device == first.device,
            )
            .values({column: digest})
        )
        self._pending_writes += 1
        if self._pending_writes >= COMMIT_EVERY_FILES:
            self.session.commit()
            self._pending_writes = 0
