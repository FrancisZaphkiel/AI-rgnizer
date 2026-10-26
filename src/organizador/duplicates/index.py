"""Escritura de resultados (hashes, metadatos de imagen) en el índice del escáner."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from typing import Any, cast

from sqlalchemy import Table, and_, bindparam, update
from sqlalchemy.orm import Session

from organizador.duplicates.hashing import FileSignature
from organizador.storage.models import File

FLUSH_EVERY_ROWS = 500

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
