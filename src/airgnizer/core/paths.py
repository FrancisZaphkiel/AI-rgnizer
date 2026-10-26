"""Rutas de la aplicación según la especificación XDG Base Directory.

Las funciones leen el entorno en cada llamada (no hay caché a nivel de módulo)
para que los tests puedan aislar ``HOME`` y ``XDG_*`` con ``monkeypatch``.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from airgnizer import APP_NAME

_USER_DIR_LINE = re.compile(r'^\s*XDG_(?P<key>[A-Z]+)_DIR\s*=\s*"(?P<value>[^"]*)"\s*$')


def _home() -> Path:
    return Path(os.environ.get("HOME") or Path.home())


def _xdg_base(env_var: str, default_relative: str) -> Path:
    # La especificación exige ignorar valores relativos.
    value = os.environ.get(env_var, "")
    if value and Path(value).is_absolute():
        return Path(value)
    return _home() / default_relative


def config_home() -> Path:
    return _xdg_base("XDG_CONFIG_HOME", ".config")


def data_home() -> Path:
    return _xdg_base("XDG_DATA_HOME", ".local/share")


def state_home() -> Path:
    return _xdg_base("XDG_STATE_HOME", ".local/state")


def cache_home() -> Path:
    return _xdg_base("XDG_CACHE_HOME", ".cache")


def user_dir(key: str) -> Path | None:
    """Devuelve un directorio de usuario (``DOWNLOAD``, ``DOCUMENTS``…) de ``user-dirs.dirs``.

    Equivale a ``xdg-user-dir`` sin lanzar un proceso. Devuelve ``None`` si no
    está definido; el llamador decide el valor por defecto.
    """
    dirs_file = config_home() / "user-dirs.dirs"
    try:
        content = dirs_file.read_text(encoding="utf-8")
    except OSError:
        return None
    for line in content.splitlines():
        match = _USER_DIR_LINE.match(line)
        if match and match["key"] == key.upper():
            raw = match["value"].replace("$HOME", str(_home()))
            path = Path(raw)
            return path if path.is_absolute() else None
    return None


def downloads_dir() -> Path:
    return user_dir("DOWNLOAD") or _home() / "Downloads"


@dataclass(frozen=True, slots=True)
class AppPaths:
    """Directorios propios de la aplicación."""

    config_dir: Path
    data_dir: Path
    state_dir: Path
    cache_dir: Path

    @classmethod
    def from_env(cls) -> AppPaths:
        return cls(
            config_dir=config_home() / APP_NAME,
            data_dir=data_home() / APP_NAME,
            state_dir=state_home() / APP_NAME,
            cache_dir=cache_home() / APP_NAME,
        )

    @property
    def config_file(self) -> Path:
        return self.config_dir / "config.toml"

    @property
    def database_file(self) -> Path:
        return self.data_dir / "airgnizer.db"

    @property
    def log_dir(self) -> Path:
        return self.state_dir / "logs"

    def ensure(self) -> None:
        """Crea los directorios con permisos privados (0700)."""
        for directory in (self.config_dir, self.data_dir, self.state_dir, self.cache_dir):
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)


class FolderSet:
    """«¿Está dentro de alguna de estas carpetas?» comparando texto, sin ``pathlib``.

    ``Path.is_relative_to`` cuesta ~0,25 ms: con 16 000 carpetas indexadas y una veintena
    de exclusiones, el servicio tardaba 100 s en arrancar. Las rutas deben venir ya
    absolutas y normalizadas (como las guarda el índice).
    """

    __slots__ = ("_everything", "_exact", "_prefixes")

    def __init__(self, folders: Iterable[str | os.PathLike[str]]) -> None:
        texts = [os.fspath(f).rstrip("/") for f in folders]
        self._everything = "" in texts  # la raíz «/» lo contiene todo
        self._exact = frozenset(texts)
        self._prefixes = tuple(t + "/" for t in texts)

    def __contains__(self, path: object) -> bool:
        if not isinstance(path, (str, os.PathLike)):
            return False
        text = os.fspath(path)
        if not isinstance(text, str):
            return False
        return self._everything or text in self._exact or text.startswith(self._prefixes)

    def __bool__(self) -> bool:
        return bool(self._exact)
