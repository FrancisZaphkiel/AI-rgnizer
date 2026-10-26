"""Configuración del usuario persistida en ``config.toml``."""

from __future__ import annotations

import logging
import os
import tomllib
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import tomli_w

from organizador.core.paths import AppPaths, downloads_dir, user_dir

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
    # Dónde buscar duplicados; vacía: en las carpetas organizadas (``indexed_dirs``).
    dirs: list[str] = field(default_factory=list)
    # Saltar proyectos de software (.git, pyproject.toml, package.json…): quitar un
    # archivo repetido de un repositorio lo rompe.
    skip_projects: bool = True


def default_type_destinations() -> dict[str, str]:
    """Destino inicial por tipo de archivo: las carpetas de usuario XDG."""
    folders = {
        "tipo:image": ("PICTURES", "Pictures"),
        "tipo:video": ("VIDEOS", "Videos"),
        "tipo:audio": ("MUSIC", "Music"),
        "ext:pdf": ("DOCUMENTS", "Documents"),
        "tipo:text": ("DOCUMENTS", "Documents"),
    }
    return {
        token: str(user_dir(key) or Path.home() / fallback)
        for token, (key, fallback) in folders.items()
    }


@dataclass(slots=True)
class LearningSettings:
    # Clase = carpeta destino truncada a esta profundidad bajo su raíz indexada.
    max_depth: int = 2
    # Carpetas con menos ejemplos se descartan: no se puede aprender de ellas.
    min_examples: int = 5
    # Límite de ejemplos por carpeta para que una muy grande no domine.
    max_examples_per_class: int = 2000
    # Por debajo de esta probabilidad calibrada no se sugiere destino («sin clasificar»).
    abstain_below: float = 0.5
    # Regla «token → carpeta»: soporte mínimo y fracción mínima de ejemplos en esa carpeta.
    rule_min_support: int = 10
    rule_min_purity: float = 0.9
    # Peso de cada corrección del usuario frente a un archivo ya organizado.
    feedback_weight: float = 5.0
    # Versiones del modelo que se conservan para poder revertir.
    keep_models: int = 5
    # «organizador review»: probabilidad mínima del modelo para proponer mover un archivo
    # que ya está organizado (más exigente que abstain_below: el usuario ya eligió).
    review_min_confidence: float = 0.9
    # Correcciones hacia una carpeta nueva que disparan un reentrenamiento automático.
    retrain_after: int = 3
    # Reglas del usuario «token → carpeta» (p. ej. "ext:jpg" = "~/Imágenes"): mandan
    # sobre todo lo aprendido. Se comprueban en el orden en que aparecen.
    rules: dict[str, str] = field(default_factory=dict)
    # Último recurso antes de «sin clasificar»: carpeta por tipo de archivo.
    type_destinations: dict[str, str] = field(default_factory=default_type_destinations)
    # Carpetas que no reflejan costumbres del usuario (salidas de programas, etc.).
    exclude: list[str] = field(default_factory=list)


WATCH_MODES = ("suggest", "auto")


@dataclass(slots=True)
class WatchSettings:
    # "suggest": solo propone destinos; "auto": mueve si la confianza llega al umbral.
    mode: str = "suggest"
    # Probabilidad calibrada mínima para mover sin preguntar (solo reglas y modelo:
    # los destinos por tipo o por nombre de carpeta no tienen probabilidad calibrada).
    auto_threshold: float = 0.9
    # Segundos sin cambios de tamaño ni mtime para dar una descarga por terminada.
    stable_seconds: float = 3.0
    # Cada cuánto se relee la carpeta entera por si inotify perdió eventos.
    reconcile_seconds: float = 300.0

    def __post_init__(self) -> None:
        if self.mode not in WATCH_MODES:
            raise ValueError(f"watch.mode debe ser uno de {WATCH_MODES}, no {self.mode!r}")


@dataclass(slots=True)
class Settings:
    indexed_dirs: list[Path] = field(default_factory=list)
    watched_dirs: list[Path] = field(default_factory=lambda: [downloads_dir()])
    scan: ScanSettings = field(default_factory=ScanSettings)
    actions: ActionSettings = field(default_factory=ActionSettings)
    duplicates: DuplicateSettings = field(default_factory=DuplicateSettings)
    learning: LearningSettings = field(default_factory=LearningSettings)
    watch: WatchSettings = field(default_factory=WatchSettings)

    def duplicate_roots(self) -> list[Path]:
        """Carpetas donde buscar duplicados: las elegidas o, si no hay, las organizadas."""
        chosen = [Path(d) for d in self.duplicates.dirs]
        return [p.expanduser() for p in (chosen or self.indexed_dirs)]

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
            learning=LearningSettings(**data.get("learning", {})),
            watch=WatchSettings(**data.get("watch", {})),
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
    _write_toml(paths, settings.to_dict())


def set_config_value(paths: AppPaths, key: str, value: Any) -> None:
    """Cambia una clave (``"watched_dirs"``, ``"watch.mode"``…) sin escribir el resto de
    valores por defecto.

    Los comentarios de ``config.toml`` se pierden (``tomli_w`` no los conserva).
    """
    try:
        with paths.config_file.open("rb") as fh:
            data = tomllib.load(fh)
    except FileNotFoundError:
        data = {}
    *parents, last = key.split(".")
    table = data
    for part in parents:
        table = table.setdefault(part, {})
    table[last] = value
    Settings.from_dict(data)  # valida antes de escribir
    _write_toml(paths, data)


def _write_toml(paths: AppPaths, data: dict[str, Any]) -> None:
    paths.ensure()
    tmp = paths.config_file.with_suffix(".toml.tmp")
    with tmp.open("wb") as fh:
        tomli_w.dump(data, fh)
        fh.flush()
        os.fsync(fh.fileno())
    tmp.replace(paths.config_file)
