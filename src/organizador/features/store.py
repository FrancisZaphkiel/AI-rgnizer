"""Características guardadas en el índice: se calculan una vez y se reutilizan.

Una fila vale mientras su firma (tamaño, ``mtime``, ``ctime``, inode, dispositivo) no
cambie —el escáner anula ``features`` en cuanto cambia— y su ``features_version``
coincida con ``FEATURES_VERSION``.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select

from organizador.features.extract import FEATURES_VERSION, FileFeatures, extract_features
from organizador.storage import Database
from organizador.storage.index import FileSignature, IndexWriter, signature, under_roots
from organizador.storage.models import Directory, File

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class IndexedFeatures:
    file_id: int
    path: Path
    features: FileFeatures


@dataclass(slots=True)
class FeatureStats:
    files: int = 0
    extracted: int = 0
    stale: int = 0
    errors: int = 0


ProgressCallback = Callable[[FeatureStats], None]


def indexed_features(
    db: Database,
    roots: Sequence[Path],
    *,
    with_metadata: bool = True,
    skip_dirs: Sequence[Path] = (),
    stats: FeatureStats | None = None,
    cancel: threading.Event | None = None,
    progress: ProgressCallback | None = None,
) -> Iterator[IndexedFeatures]:
    """Características de los archivos indexados bajo ``roots``; calcula las que faltan.

    Los archivos que cambiaron desde el último escaneo o que no se pueden leer se omiten,
    igual que los que están dentro de ``skip_dirs`` (sin llegar a abrirlos).
    """
    stats = stats if stats is not None else FeatureStats()
    resolved = [r.expanduser().resolve() for r in roots]
    skipped = frozenset(skip_dirs)

    def is_skipped(directory: Path) -> bool:
        # Recorre los ancestros (pocos) en lugar de comparar con cada carpeta excluida.
        return any(d in skipped for d in (directory, *directory.parents))

    with db.new_session() as session:
        rows = session.execute(
            select(
                File.id,
                Directory.path,
                File.name,
                File.size,
                File.mtime_ns,
                File.ctime_ns,
                File.inode,
                File.device,
                File.features,
                File.features_version,
            )
            .join(Directory)
            .where(under_roots(resolved))
            .order_by(Directory.path, File.name)
        ).all()
        writer = IndexWriter(session)
        for fid, dir_path, name, size, mtime, ctime, ino, dev, raw, version in rows:
            if cancel is not None and cancel.is_set():
                break
            path = Path(dir_path) / name
            if skipped and is_skipped(path.parent):
                continue
            stats.files += 1
            if raw is not None and version == FEATURES_VERSION:
                yield IndexedFeatures(fid, path, FileFeatures.from_json(raw))
                continue
            sig: FileSignature = (size, mtime, ctime, ino, dev)
            features = _extract_if_unchanged(path, sig, with_metadata, stats)
            if features is None:
                continue
            # Sin metadatos la extracción está incompleta: no se guarda como definitiva.
            if with_metadata:
                writer.queue(
                    [fid],
                    sig,
                    {"features": features.to_json(), "features_version": FEATURES_VERSION},
                )
            if progress is not None:
                progress(stats)
            yield IndexedFeatures(fid, path, features)
        writer.flush()
        session.commit()


def _extract_if_unchanged(
    path: Path, sig: FileSignature, with_metadata: bool, stats: FeatureStats
) -> FileFeatures | None:
    try:
        if signature(path.lstat()) != sig:
            stats.stale += 1
            return None
        features = extract_features(path, with_metadata=with_metadata)
        if signature(path.lstat()) != sig:  # cambió mientras se leía
            stats.stale += 1
            return None
    except OSError as exc:
        log.debug("No se pudieron extraer características de %s: %s", path, exc)
        stats.errors += 1
        return None
    stats.extracted += 1
    return features
