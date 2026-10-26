"""Utilidades comunes sobre el índice del escáner: firma de archivo, filtro por raíces y
escritura por lotes de valores derivados (hashes, metadatos, características)."""

from __future__ import annotations

import os
from collections import defaultdict
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, cast

from sqlalchemy import ColumnElement, Table, and_, bindparam, false, or_, update
from sqlalchemy.orm import Session

from airgnizer.storage.models import Directory, File

FLUSH_EVERY_ROWS = 500

# (tamaño, mtime_ns, ctime_ns, inode, dispositivo): si cambia, lo derivado del archivo ya no vale.
FileSignature = tuple[int, int, int, int, int]


def signature(st: os.stat_result) -> FileSignature:
    """Misma firma que usa el escáner para decidir si un archivo cambió."""
    return (st.st_size, st.st_mtime_ns, st.st_ctime_ns, st.st_ino, st.st_dev)


def under_roots(roots: Sequence[Path]) -> ColumnElement[bool]:
    """Condición SQL: la carpeta es una de ``roots`` o está dentro de alguna."""
    clauses: list[ColumnElement[bool]] = []
    for root in roots:
        prefix = str(root).rstrip("/") + "/"
        clauses.append(Directory.path == str(root))
        clauses.append(Directory.path.startswith(prefix, autoescape=True))
    return or_(false(), *clauses)  # sin raíces no coincide nada


# Sobre la tabla (Core), sin la sincronización de sesión del ORM, que era el cuello de botella.
_files = cast("Table", File.__table__)
_SIGNATURE_COLUMNS = ("size", "mtime_ns", "ctime_ns", "inode", "device")


class IndexWriter:
    """Acumula actualizaciones y las escribe por lotes con una sola sentencia preparada.

    Cada fila solo se actualiza si su firma sigue siendo la registrada al calcular el
    valor: así no se pisa una fila que el escáner acaba de actualizar con un resultado
    obtenido de una versión anterior del archivo.
    """

    def __init__(self, session: Session) -> None:
        self._session = session
        # Lotes por conjunto de columnas: executemany exige los mismos parámetros por fila.
        self._pending: dict[tuple[str, ...], list[dict[str, Any]]] = defaultdict(list)
        self._count = 0

    def queue(self, file_ids: Sequence[int], sig: FileSignature, values: Mapping[str, Any]) -> None:
        columns = tuple(sorted(values))
        signature = dict(zip((f"sig_{c}" for c in _SIGNATURE_COLUMNS), sig, strict=True))
        for file_id in file_ids:
            self._pending[columns].append(
                {"sig_id": file_id, **signature, **{f"new_{c}": values[c] for c in columns}}
            )
            self._count += 1
        if self._count >= FLUSH_EVERY_ROWS:
            # Se confirma por lotes: si se cancela o falla, lo ya calculado se conserva.
            self.flush()
            self._session.commit()

    def flush(self) -> None:
        for columns, rows in self._pending.items():
            statement = (
                update(_files)
                .where(
                    and_(
                        _files.c.id == bindparam("sig_id"),
                        *(_files.c[c] == bindparam(f"sig_{c}") for c in _SIGNATURE_COLUMNS),
                    )
                )
                .values({c: bindparam(f"new_{c}") for c in columns})
            )
            self._session.execute(statement, rows)
        self._pending.clear()
        self._count = 0
