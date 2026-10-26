"""¿Ha terminado de descargarse un archivo?

Tres comprobaciones, de la más barata a la más general:

1. **Temporales conocidos**, que se renombran al acabar:

   - Firefox, LibreWolf, Tor Browser, Floorp, Zen: marcador vacío ``nombre.ext`` y
     descarga en ``nombre.ext.part``;
   - Chrome, Chromium, Brave, Edge, Vivaldi, Opera y los basados en QtWebEngine
     (Falkon, qutebrowser, Konqueror): ``nombre.ext.crdownload`` o
     ``Unconfirmed NNN.crdownload``, más temporales ocultos ``.com.google.Chrome.*``;
   - Opera antiguo ``.opdownload``; GNOME Web (WebKitGTK) ``.wkdownload``;
     KGet ``.part``/``.kget``; DownThemAll ``.dtapart``; WinSCP ``.filepart``;
   - qBittorrent ``.!qB``, µTorrent ``.!ut``; aria2 deja ``nombre.aria2`` **junto al**
     archivo, que ya tiene su tamaño final por la prealocación.

2. **Estabilidad**: el tamaño y el mtime no cambian durante ``stable_seconds``
   (wget, curl, copias desde un pendrive…).

3. **Nadie lo está escribiendo**: ningún proceso del usuario lo tiene abierto para
   escritura (``/proc/PID/fdinfo``). Cubre a quien descarga directamente en el nombre
   final, incluso con el tamaño ya prealocado.
"""

from __future__ import annotations

import stat
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

# En minúsculas: la comparación ignora mayúsculas (``.!qB``, ``.PART``).
TEMPORARY_SUFFIXES = frozenset(
    {
        ".part",
        ".crdownload",
        ".opdownload",
        ".wkdownload",
        ".kget",
        ".partial",
        ".download",
        ".dtapart",
        ".filepart",
        ".!qb",
        ".!ut",
        ".aria2",
        ".tmp",
        ".crswap",
    }
)
PROC = Path("/proc")
# Modo de acceso en «flags» de fdinfo (octal): O_WRONLY = 1, O_RDWR = 2.
_ACCESS_MODE = 0o3


def is_temporary(path: Path) -> bool:
    # Los ocultos son de programas (``.~lock``, ``.goutputstream-*``); «...» sí es visible.
    hidden = path.name.startswith(".") and not path.name.startswith("...")
    return hidden or path.suffix.lower() in TEMPORARY_SUFFIXES or path.name.endswith("~")


def has_partial_sibling(path: Path) -> bool:
    """Hay una descarga en curso con este nombre (``nombre.ext.part``, ``nombre.ext.aria2``…)."""
    try:
        siblings = {p.name.lower() for p in path.parent.iterdir()}
    except OSError:
        return False
    base = path.name.lower()
    return any(base + suffix in siblings for suffix in TEMPORARY_SUFFIXES)


FileId = tuple[int, int]  # (st_dev, st_ino)


def files_open_for_writing(proc: Path = PROC) -> set[FileId]:
    """Archivos que algún proceso visible tiene abiertos para escritura.

    Solo se ven los procesos del propio usuario (los demás dan ``PermissionError``),
    que son los que descargan en su carpeta. Se lee ``/proc`` una vez para todos los
    candidatos de una pasada.
    """
    try:
        pids = [p for p in proc.iterdir() if p.name.isdigit()]
    except OSError:
        return set()
    found: set[FileId] = set()
    for pid in pids:
        try:
            fds = list((pid / "fd").iterdir())
        except OSError:
            continue
        for fd in fds:
            try:
                info = (pid / "fdinfo" / fd.name).read_text()
                flags = next(line for line in info.splitlines() if line.startswith("flags:"))
                if not int(flags.split()[1], 8) & _ACCESS_MODE:
                    continue
                st = fd.stat()
            except (OSError, StopIteration, ValueError):
                continue
            found.add((st.st_dev, st.st_ino))
    return found


def open_for_writing(path: Path, proc: Path = PROC) -> bool:
    """Algún proceso visible tiene ``path`` abierto para escritura."""
    try:
        st = path.stat()
    except OSError:
        return False
    return (st.st_dev, st.st_ino) in files_open_for_writing(proc)


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
        # Sin el inodo: en FAT/exFAT cambia al volver a montar el disco.
        return (self.size, self.mtime_ns) == (other.size, other.mtime_ns)


@dataclass(slots=True)
class StabilityTracker:
    """Da por terminados los archivos que no cambian durante ``stable_seconds``."""

    stable_seconds: float
    # Archivos abiertos para escritura; inyectable en tests (por defecto, /proc).
    writers: Callable[[], set[FileId]] = files_open_for_writing
    # Llegadas que ya tienen sugerencia: se descartan antes de cualquier trabajo.
    is_known: Callable[[Arrival], bool] = lambda _a: False
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
        stable: list[Arrival] = []
        for path, (arrival, since) in list(self._seen.items()):
            if now - since < self.stable_seconds:
                continue
            current = Arrival.of(path)
            if current is None or self.is_known(current):
                del self._seen[path]
            elif not current.same_content(arrival):
                self._seen[path] = (current, now)  # sigue creciendo
            elif has_partial_sibling(path):
                self._seen[path] = (current, now)  # marcador de Firefox o similar
            else:
                stable.append(current)
        if not stable:
            return []
        writing = self.writers()
        out: list[Arrival] = []
        for arrival in stable:
            if (arrival.device, arrival.inode) in writing:
                self._seen[arrival.path] = (arrival, now)  # alguien sigue escribiendo
            else:
                del self._seen[arrival.path]
                out.append(arrival)
        return out
