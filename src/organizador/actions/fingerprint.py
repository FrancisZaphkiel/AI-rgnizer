"""Huella de un archivo para verificar que sigue siendo el mismo antes de actuar."""

from __future__ import annotations

import stat
from dataclasses import dataclass
from pathlib import Path

from organizador.actions.fsops import hash_file

# FAT guarda el mtime con 2 s de resolución y exFAT con 10 ms: al mover a un disco
# externo el mtime copiado se redondea.
MTIME_TOLERANCE_NS = 2_000_000_000


@dataclass(frozen=True, slots=True)
class Fingerprint:
    size: int
    mtime_ns: int
    inode: int
    device: int
    content_hash: str | None

    @classmethod
    def of(cls, path: Path, *, hash_max_bytes: int | None) -> Fingerprint:
        """Toma la huella; el hash solo se calcula si ``size <= hash_max_bytes``."""
        st = path.lstat()
        if not stat.S_ISREG(st.st_mode):
            raise IsADirectoryError(f"Solo se admiten archivos regulares: {path}")
        with_hash = hash_max_bytes is None or st.st_size <= hash_max_bytes
        return cls(
            size=st.st_size,
            mtime_ns=st.st_mtime_ns,
            inode=st.st_ino,
            device=st.st_dev,
            content_hash=hash_file(path) if with_hash else None,
        )

    def matches(self, path: Path) -> bool:
        """¿Es ``path`` el mismo contenido? No compara inode: cambia al cruzar dispositivos.

        ``mtime`` se compara porque se conserva al mover (rename o ``copystat``), con la
        tolerancia de ``MTIME_TOLERANCE_NS`` para sistemas de archivos de baja resolución.
        """
        try:
            st = path.lstat()
        except FileNotFoundError:
            return False
        if not stat.S_ISREG(st.st_mode) or st.st_size != self.size:
            return False
        if abs(st.st_mtime_ns - self.mtime_ns) > MTIME_TOLERANCE_NS:
            return False
        return self.content_hash is None or hash_file(path) == self.content_hash
