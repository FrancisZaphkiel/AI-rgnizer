"""Configuración del usuario persistida en ``config.toml``."""

from __future__ import annotations

import logging
import os
import tomllib
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import tomli_w

from organizador.core.paths import AppPaths, downloads_dir

log = logging.getLogger(__name__)

DEFAULT_EXCLUDED_NAMES: tuple[str, ...] = (
    ".git",
    ".hg",
    ".svn",
    "node_modules",
    "__pycache__",
    ".venv",
    ".cache",
)


@dataclass(slots=True)
class ScanSettings:
    include_hidden: bool = False
    follow_symlinks: bool = False
    cross_mounts: bool = False
    excluded_names: list[str] = field(default_factory=lambda: list(DEFAULT_EXCLUDED_NAMES))
    excluded_globs: list[str] = field(default_factory=lambda: [".Trash-*"])


@dataclass(slots=True)
class ActionSettings:
    # Por encima de este tamaño no se calcula el hash completo al mover dentro
    # del mismo sistema de archivos (el rename no altera el contenido).
    fingerprint_hash_max_bytes: int = 256 * 1024 * 1024


@dataclass(slots=True)
class DuplicateSettings:
    # Los archivos vacíos (o diminutos) no liberan espacio: no se buscan por defecto.
    min_size: int = 1
    # Bytes leídos del principio y del final para el hash parcial (solo filtro).
    partial_bytes: int = 64 * 1024
    # Distancia de Hamming máxima entre pHash de 64 bits para proponer imágenes similares.
    # Calibrado: redimensionar o recomprimir da ≤ 2; fotos distintas, ≥ 20; capturas de
    # pantalla distintas de una misma aplicación, desde 0 (por eso son solo candidatos).
    image_max_distance: int = 4


@dataclass(slots=True)
class Settings:
    indexed_dirs: list[Path] = field(default_factory=list)
    watched_dirs: list[Path] = field(default_factory=lambda: [downloads_dir()])
    scan: ScanSettings = field(default_factory=ScanSettings)
    actions: ActionSettings = field(default_factory=ActionSettings)
    duplicates: DuplicateSettings = field(default_factory=DuplicateSettings)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["indexed_dirs"] = [str(p) for p in self.indexed_dirs]
        data["watched_dirs"] = [str(p) for p in self.watched_dirs]
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Settings:
        defaults = cls()
        return cls(
            indexed_dirs=[Path(p) for p in data.get("indexed_dirs", [])],
            watched_dirs=[Path(p) for p in data.get("watched_dirs", defaults.watched_dirs)],
            scan=ScanSettings(**data.get("scan", {})),
            actions=ActionSettings(**data.get("actions", {})),
            duplicates=DuplicateSettings(**data.get("duplicates", {})),
        )


def load_settings(paths: AppPaths) -> Settings:
    """Carga la configuración; si no existe o es inválida, devuelve los valores por defecto."""
    try:
        with paths.config_file.open("rb") as fh:
            return Settings.from_dict(tomllib.load(fh))
    except FileNotFoundError:
        return Settings()
    except (tomllib.TOMLDecodeError, TypeError, ValueError) as exc:
        log.warning(
            "Configuración inválida en %s (%s); se usan valores por defecto", paths.config_file, exc
        )
        return Settings()


def save_settings(paths: AppPaths, settings: Settings) -> None:
    """Escribe la configuración de forma atómica (archivo temporal + rename)."""
    paths.ensure()
    tmp = paths.config_file.with_suffix(".toml.tmp")
    with tmp.open("wb") as fh:
        tomli_w.dump(settings.to_dict(), fh)
        fh.flush()
        os.fsync(fh.fileno())
    tmp.replace(paths.config_file)
