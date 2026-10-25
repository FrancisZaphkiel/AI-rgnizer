"""Hashes de contenido para la detección de duplicados.

- ``partial_hash``: xxh3-128 del tamaño más el principio y el final del archivo. Es
  **solo un filtro** para descartar candidatos; nunca prueba que dos archivos sean iguales.
- ``full_hash``: BLAKE3 del contenido completo; la única confirmación válida.
"""

from __future__ import annotations

import os
from pathlib import Path

import xxhash

from organizador.actions.fsops import hash_file

FileSignature = tuple[int, int, int, int, int]


def signature(st: os.stat_result) -> FileSignature:
    """Misma firma que usa el escáner: si cambia, el hash guardado ya no vale."""
    return (st.st_size, st.st_mtime_ns, st.st_ctime_ns, st.st_ino, st.st_dev)


def needs_partial(size: int, partial_bytes: int) -> bool:
    """Si el archivo cabe en las dos ventanas, el hash parcial no ahorra lecturas."""
    return size > 2 * partial_bytes


def partial_hash(path: Path, size: int, partial_bytes: int) -> str:
    hasher = xxhash.xxh3_128()
    hasher.update(size.to_bytes(8, "little"))
    with path.open("rb") as fh:
        hasher.update(fh.read(partial_bytes))
        fh.seek(max(0, size - partial_bytes))
        hasher.update(fh.read(partial_bytes))
    return hasher.hexdigest()


def full_hash(path: Path) -> str:
    return hash_file(path)
