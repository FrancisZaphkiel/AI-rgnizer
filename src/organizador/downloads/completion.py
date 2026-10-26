"""¿Ha terminado de descargarse un archivo?

Los navegadores escriben en un temporal y lo renombran al acabar:

- Firefox crea ``nombre.ext`` vacío (marcador) y descarga en ``nombre.ext.part``;
- Chromium descarga en ``nombre.ext.crdownload`` (o ``Unconfirmed NNN.crdownload``);
- KGet usa ``.part`` o ``.kget``.

Además de reconocer temporales se exige estabilidad: el tamaño y el mtime no deben
cambiar durante ``stable_seconds`` (descargas con wget, copias desde un pendrive…).
"""

from __future__ import annotations

import stat
from dataclasses import dataclass, field
from pathlib import Path

TEMPORARY_SUFFIXES = frozenset({".part", ".crdownload", ".kget", ".partial", ".download"})


def is_temporary(path: Path) -> bool:
    # Los ocultos son de programas (``.~lock``, ``.goutputstream-*``); «...» sí es visible.
    hidden = path.name.startswith(".") and not path.name.startswith("...")
    return hidden or path.suffix.lower() in TEMPORARY_SUFFIXES or path.name.endswith("~")


def has_partial_sibling(path: Path) -> bool:
    """El archivo es el marcador de una descarga en curso (``nombre.ext.part`` existe)."""
    return any(path.with_name(path.name + s).exists() for s in TEMPORARY_SUFFIXES)


@dataclass(frozen=True, slots=True)
class Arrival:
    """Huella de un archivo en la carpeta vigilada."""

    path: Path
    size: int
    mtime_ns: int
    inode: int
    device: int

    @classmethod
    def of(cls, path: Path) -> Arrival | None:
        """Huella de ``path`` si es un archivo regular candidato; si no, ``None``."""
        if is_temporary(path):
            return None
        try:
            st = path.lstat()
        except OSError:
            return None
        if not stat.S_ISREG(st.st_mode):
            return None
        return cls(path, st.st_size, st.st_mtime_ns, st.st_ino, st.st_dev)

    def same_content(self, other: Arrival) -> bool:
        return (self.size, self.mtime_ns, self.inode) == (other.size, other.mtime_ns, other.inode)


@dataclass(slots=True)
class StabilityTracker:
    """Da por terminados los archivos que no cambian durante ``stable_seconds``."""

    stable_seconds: float
    _seen: dict[Path, tuple[Arrival, float]] = field(default_factory=dict)

    def notice(self, path: Path, now: float) -> None:
        """Un evento o la reconciliación vio ``path``; reinicia su espera si cambió."""
        arrival = Arrival.of(path)
        if arrival is None:
            self._seen.pop(path, None)
            return
        previous = self._seen.get(path)
        if previous is None or not previous[0].same_content(arrival):
            self._seen[path] = (arrival, now)

    def forget(self, path: Path) -> None:
        self._seen.pop(path, None)

    def __len__(self) -> int:
        return len(self._seen)

    def ready(self, now: float) -> list[Arrival]:
        """Archivos estables y completos; dejan de seguirse al devolverlos."""
        out: list[Arrival] = []
        for path, (arrival, since) in list(self._seen.items()):
            if now - since < self.stable_seconds:
                continue
            current = Arrival.of(path)
            if current is None:
                del self._seen[path]
            elif not current.same_content(arrival):
                self._seen[path] = (current, now)  # sigue creciendo
            elif has_partial_sibling(path):
                self._seen[path] = (current, now)  # marcador de Firefox: aún no
            else:
                del self._seen[path]
                out.append(current)
        return out
