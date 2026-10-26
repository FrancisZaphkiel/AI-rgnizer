"""Consultas de solo lectura para la GUI (y quien las necesite).

Leer la base es seguro con el servicio en marcha (WAL: lectores concurrentes con un
escritor); todo lo que escribe va por ``backend``.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import func, not_, select

from organizador.app import AppContext, duplicate_exclude, folder_pattern, learning_exclude
from organizador.core.paths import FolderSet
from organizador.learning.dataset import find_project_dirs
from organizador.learning.patterns import FolderPattern
from organizador.learning.store import ModelInfo
from organizador.storage.index import under_roots
from organizador.storage.models import (
    ActionJournal,
    Directory,
    DownloadSuggestion,
    Feedback,
    File,
    NamingPattern,
    SuggestionState,
)


def pending_suggestions(ctx: AppContext) -> list[DownloadSuggestion]:
    with ctx.db.session() as s:
        return list(
            s.scalars(
                select(DownloadSuggestion)
                .where(DownloadSuggestion.state == SuggestionState.PENDING)
                .order_by(DownloadSuggestion.id)
            )
        )


def history(ctx: AppContext, limit: int = 200) -> list[ActionJournal]:
    with ctx.db.session() as s:
        return list(
            s.scalars(select(ActionJournal).order_by(ActionJournal.created_at.desc()).limit(limit))
        )


@dataclass(frozen=True, slots=True)
class Accuracy:
    accepted: int
    total: int

    @property
    def fraction(self) -> float | None:
        return self.accepted / self.total if self.total else None


def suggestion_accuracy(ctx: AppContext, last: int = 200) -> Accuracy:
    """Qué parte de las últimas respuestas confirmó la sugerencia tal cual.

    Mide el modelo con lo que de verdad decidiste, no con una partición de prueba.
    """
    with ctx.db.session() as s:
        recent = (
            select(Feedback.accepted)
            .where(Feedback.suggested.is_not(None))
            .order_by(Feedback.id.desc())
            .limit(last)
            .subquery()
        )
        total, accepted = s.execute(
            select(func.count(), func.coalesce(func.sum(recent.c.accepted), 0))
        ).one()
    return Accuracy(int(accepted), int(total))


@dataclass(frozen=True, slots=True)
class Recoverable:
    groups: int
    bytes: int


def recoverable_from_index(ctx: AppContext, roots: Sequence[Path] | None = None) -> Recoverable:
    """Espacio que liberarían los duplicados exactos ya identificados en el índice.

    Usa los hashes guardados por la última búsqueda (no lee archivos); los enlaces duros
    cuentan una sola vez porque no ocupan más.
    """
    roots = list(roots) if roots is not None else ctx.settings.duplicate_roots()
    if not roots:
        return Recoverable(0, 0)
    with ctx.db.session() as s:
        copies = (
            select(
                File.content_hash,
                File.size,
                func.count(func.distinct(File.device * 4294967296 + File.inode)).label("n"),
            )
            .join(Directory)
            .where(
                File.content_hash.is_not(None),
                File.size > 0,
                under_roots(roots),
                not_(under_roots(duplicate_exclude(ctx, roots))),
            )
            .group_by(File.content_hash, File.size)
            .subquery()
        )
        groups, total = s.execute(
            select(func.count(), func.coalesce(func.sum((copies.c.n - 1) * copies.c.size), 0))
            .select_from(copies)
            .where(copies.c.n > 1)
        ).one()
    return Recoverable(int(groups), int(total))


_model_info_cache: dict[tuple[str, int], ModelInfo] = {}


def model_info(ctx: AppContext) -> ModelInfo | None:
    """El modelo activo; se lee solo ese archivo y solo cuando cambia."""
    store = ctx.models()
    version = store.current_version()
    if version is None:
        return None
    path = store.path_of(version)
    try:
        key = (str(path), path.stat().st_mtime_ns)
    except OSError:
        return None
    if key not in _model_info_cache:
        _model_info_cache.clear()
        info = store.info(version)
        if info is None:
            return None
        _model_info_cache[key] = info
    return _model_info_cache[key]


@dataclass(frozen=True, slots=True)
class FolderPatternRow:
    folder: str
    pattern: FolderPattern | None
    edited: bool
    enabled: bool


def pattern_rows(ctx: AppContext) -> list[FolderPatternRow]:
    """Carpetas indexadas con un patrón claro o editado por el usuario."""
    roots = [r.expanduser() for r in ctx.settings.indexed_dirs if r.expanduser().is_dir()]
    with ctx.db.session() as s:
        rows = (
            s.execute(select(Directory.path, File.name).join(File).where(under_roots(roots))).all()
            if roots
            else []
        )
        edited = {
            p.directory: p
            for p in s.scalars(select(NamingPattern).where(NamingPattern.user_edited.is_(True)))
        }
    names: dict[str, list[str]] = {}
    for folder, name in rows:
        names.setdefault(folder, []).append(name)
    skipped = FolderSet(
        [
            *(p.expanduser().resolve() for p in learning_exclude(ctx)),
            *(find_project_dirs(ctx.db, roots) if roots else []),
        ]
    )
    out: list[FolderPatternRow] = []
    for folder in sorted(set(names) | set(edited)):
        if folder in skipped:
            continue
        found = folder_pattern(ctx, folder, names.get(folder, []))
        row = edited.get(folder)
        if found is None and row is None:
            continue
        out.append(FolderPatternRow(folder, found, row is not None, row.enabled if row else True))
    return out
