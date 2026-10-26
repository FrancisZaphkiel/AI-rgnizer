"""Puntos de montaje del sistema.

No se usa ``os.path.ismount`` porque en Btrfs cada subvolumen tiene su propio
``st_dev`` y sería tratado como un montaje distinto aunque no lo sea.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

MOUNTINFO = Path("/proc/self/mountinfo")


def _unescape(field: str) -> str:
    # mountinfo escapa espacios, tabs, saltos de línea y '\' en octal (\040…).
    out: list[str] = []
    i = 0
    while i < len(field):
        if field[i] == "\\" and i + 3 < len(field) and field[i + 1 : i + 4].isdigit():
            out.append(chr(int(field[i + 1 : i + 4], 8)))
            i += 4
        else:
            out.append(field[i])
            i += 1
    return "".join(out)


def mount_points(mountinfo: Path = MOUNTINFO) -> frozenset[Path]:
    """Devuelve los puntos de montaje; conjunto vacío si no se puede leer."""
    try:
        lines = mountinfo.read_text(encoding="utf-8").splitlines()
    except OSError:
        return frozenset()
    points: set[Path] = set()
    for line in lines:
        fields = line.split(" ")
        if len(fields) > 4:
            points.add(Path(_unescape(fields[4])))
    return frozenset(points)


@dataclass(frozen=True, slots=True)
class MountEntry:
    point: Path
    fstype: str
    source: str

    @property
    def is_network(self) -> bool:
        """inotify no ve los cambios hechos desde otro equipo: hay que sondear."""
        return self.fstype in NETWORK_FSTYPES or self.fstype.startswith(
            ("fuse.sshfs", "fuse.rclone", "fuse.gvfsd")
        )


NETWORK_FSTYPES = frozenset({"nfs", "nfs4", "cifs", "smb3", "smbfs", "9p", "afs", "ceph"})


def mounts(mountinfo: Path = MOUNTINFO) -> list[MountEntry]:
    try:
        lines = mountinfo.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out: list[MountEntry] = []
    for line in lines:
        # «… punto opciones [opcionales…] - tipo origen opciones_sb»
        head, sep, tail = line.partition(" - ")
        fields, rest = head.split(" "), tail.split(" ")
        if sep and len(fields) > 4 and len(rest) >= 2:
            out.append(MountEntry(Path(_unescape(fields[4])), rest[0], _unescape(rest[1])))
    return out


def filesystem_of(path: Path, mountinfo: Path = MOUNTINFO) -> MountEntry | None:
    """Montaje que contiene ``path`` (el punto de montaje más largo que es su prefijo)."""
    path = path.absolute()
    best: MountEntry | None = None
    for entry in mounts(mountinfo):
        if path.is_relative_to(entry.point) and (
            best is None or len(entry.point.parts) >= len(best.point.parts)
        ):
            best = entry
    return best
