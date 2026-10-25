"""Puntos de montaje del sistema.

No se usa ``os.path.ismount`` porque en Btrfs cada subvolumen tiene su propio
``st_dev`` y sería tratado como un montaje distinto aunque no lo sea.
"""

from __future__ import annotations

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
