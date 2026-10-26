"""Paso del nombre antiguo («organizador») a AI-rgnizer sin perder datos.

Hasta la 0.3.0 la aplicación se llamaba «organizador»: sus carpetas XDG, la base
``organizador.db``, la unidad ``organizador.service``, la activación D-Bus y la entrada
de inicio llevaban ese nombre. Al arrancar por primera vez con el nombre nuevo se
**renombran** las carpetas (no se copia nada: es instantáneo y no duplica espacio).

No se migra si el servicio antiguo sigue en marcha (tendría la base abierta): se pide
detenerlo antes.
"""

from __future__ import annotations

import fcntl
import logging
import os
from dataclasses import dataclass
from pathlib import Path

from airgnizer.core.paths import AppPaths, cache_home, config_home, data_home, state_home

log = logging.getLogger(__name__)

LEGACY_NAME = "organizador"
LEGACY_DATABASE = "organizador.db"
LEGACY_UNIT = "organizador.service"
LEGACY_BUS_NAME = "io.github.organizador.Organizador"
LEGACY_AUTOSTART = "io.github.organizador.Organizador.desktop"


class LegacyServiceRunningError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class Moved:
    old: Path
    new: Path


def legacy_dirs(paths: AppPaths) -> list[tuple[Path, Path]]:
    return [
        (config_home() / LEGACY_NAME, paths.config_dir),
        (data_home() / LEGACY_NAME, paths.data_dir),
        (state_home() / LEGACY_NAME, paths.state_dir),
        (cache_home() / LEGACY_NAME, paths.cache_dir),
    ]


def _legacy_service_running() -> bool:
    lock = state_home() / LEGACY_NAME / "daemon.lock"
    try:
        fd = os.open(lock, os.O_RDWR)
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    finally:
        os.close(fd)  # cerrar suelta el flock si se tomó
    return False


def migrate(paths: AppPaths) -> list[Moved]:
    """Renombra lo que quede con el nombre antiguo; devuelve qué se movió."""
    pending = [(old, new) for old, new in legacy_dirs(paths) if old.is_dir() and not new.exists()]
    if not pending:
        return []
    if _legacy_service_running():
        raise LegacyServiceRunningError(
            f"El servicio antiguo sigue en marcha: detenlo con «systemctl --user disable --now "
            f"{LEGACY_UNIT}» y vuelve a abrir la aplicación para trasladar tus datos."
        )
    moved: list[Moved] = []
    for old, new in pending:
        new.parent.mkdir(parents=True, exist_ok=True)
        try:
            old.rename(new)
        except OSError as exc:  # p. ej. XDG_*_HOME en otro sistema de archivos
            log.warning("No se pudo trasladar %s a %s: %s", old, new, exc)
            continue
        moved.append(Moved(old, new))
    # La base cambia de nombre con sus archivos WAL (deben ir juntos).
    for suffix in ("", "-wal", "-shm"):
        old = paths.data_dir / f"{LEGACY_DATABASE}{suffix}"
        new = paths.data_dir / f"{paths.database_file.name}{suffix}"
        if old.exists() and not new.exists():
            old.rename(new)
            moved.append(Moved(old, new))
    for item in moved:
        log.info("Trasladado %s → %s", item.old, item.new)
    return moved
