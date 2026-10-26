"""Escritura de resultados (hashes, metadatos de imagen) en el índice del escáner."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from sqlalchemy import update
from sqlalchemy.orm import Session

from organizador.duplicates.hashing import FileSignature
from organizador.storage.models import File


def store_if_unchanged(
    session: Session,
    file_ids: Sequence[int],
    sig: FileSignature,
    values: Mapping[str, Any],
) -> None:
    """Actualiza las filas solo si su firma sigue siendo ``sig``.

    Evita pisar una fila que el escáner acaba de actualizar con un valor calculado
    sobre una versión anterior del archivo.
    """
    size, mtime_ns, ctime_ns, inode, device = sig
    session.execute(
        update(File)
        .where(
            File.id.in_(file_ids),
            File.size == size,
            File.mtime_ns == mtime_ns,
            File.ctime_ns == ctime_ns,
            File.inode == inode,
            File.device == device,
        )
        .values(dict(values))
    )
