"""Versiones del modelo en disco: guardar, cargar, listar y revertir.

Cada entrenamiento crea ``model-NNNN.joblib`` en ``<data_dir>/models`` y ``current``
apunta a la versión activa. Se conservan las ``keep_models`` más recientes (y siempre la
activa), para poder volver atrás si un reentrenamiento empeora las sugerencias.

``joblib`` usa *pickle*: solo se cargan archivos de la carpeta de datos de la propia
aplicación (permisos 0700), nunca modelos de otra procedencia.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import joblib
import sklearn

from organizador.actions.fsops import discard_model_version
from organizador.features import FEATURES_VERSION
from organizador.learning.model import DestinationModel

MODEL_FORMAT = 1
_VERSION = re.compile(r"model-(\d{4,})\.joblib")


class ModelNotFoundError(LookupError):
    pass


class IncompatibleModelError(ValueError):
    """El modelo se guardó con otro formato, otra versión de las características o de
    scikit-learn: hay que volver a entrenar."""


@dataclass(frozen=True, slots=True)
class ModelInfo:
    version: int
    path: Path
    trained_at: datetime
    n_examples: int
    n_classes: int
    n_rules: int
    current: bool


class ModelStore:
    def __init__(self, directory: Path, *, keep: int = 5) -> None:
        self.directory = directory
        self.keep = max(1, keep)
        self._current_file = directory / "current"

    def _path(self, version: int) -> Path:
        return self.directory / f"model-{version:04d}.joblib"

    def _versions(self) -> list[int]:
        if not self.directory.is_dir():
            return []
        found = (_VERSION.fullmatch(p.name) for p in self.directory.iterdir())
        return sorted(int(m[1]) for m in found if m)

    def current_version(self) -> int | None:
        try:
            version = int(self._current_file.read_text().strip())
        except (FileNotFoundError, ValueError):
            return None
        return version if self._path(version).exists() else None

    def _set_current(self, version: int) -> None:
        tmp = self._current_file.with_suffix(".tmp")
        tmp.write_text(f"{version}\n")
        tmp.replace(self._current_file)  # atómico: nunca queda a medias

    def save(self, model: DestinationModel) -> int:
        """Guarda una versión nueva, la activa y elimina las que sobran."""
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        versions = self._versions()
        version = (versions[-1] + 1) if versions else 1
        payload = {
            "format": MODEL_FORMAT,
            "features_version": FEATURES_VERSION,
            "sklearn": sklearn.__version__,
            "model": model,
        }
        tmp = self._path(version).with_suffix(".tmp")
        joblib.dump(payload, tmp, compress=3)
        tmp.replace(self._path(version))
        self._set_current(version)
        self._prune()
        return version

    def _prune(self) -> None:
        current = self.current_version()
        for version in self._versions()[: -self.keep]:
            if version != current:
                discard_model_version(self._path(version), self.directory)

    def load(self, version: int | None = None) -> DestinationModel:
        version = version if version is not None else self.current_version()
        if version is None or not self._path(version).exists():
            raise ModelNotFoundError("No hay un modelo entrenado: ejecuta «organizador train»")
        payload: dict[str, Any] = joblib.load(self._path(version))
        if payload.get("format") != MODEL_FORMAT:
            raise IncompatibleModelError("Formato de modelo desconocido")
        if payload.get("features_version") != FEATURES_VERSION:
            raise IncompatibleModelError("Las características cambiaron desde el entrenamiento")
        if payload.get("sklearn") != sklearn.__version__:
            raise IncompatibleModelError(
                f"Modelo creado con scikit-learn {payload.get('sklearn')}, "
                f"instalado {sklearn.__version__}"
            )
        model = payload["model"]
        if not isinstance(model, DestinationModel):
            raise IncompatibleModelError("El archivo no contiene un modelo de destino")
        return model

    def revert(self, version: int) -> None:
        if not self._path(version).exists():
            raise ModelNotFoundError(f"No existe la versión {version}")
        self.load(version)  # comprueba que es compatible antes de activarla
        self._set_current(version)

    def list(self) -> list[ModelInfo]:
        current = self.current_version()
        infos: list[ModelInfo] = []
        for version in self._versions():
            try:
                model = self.load(version)
            except (IncompatibleModelError, OSError, EOFError):
                continue
            infos.append(
                ModelInfo(
                    version=version,
                    path=self._path(version),
                    trained_at=model.trained_at,
                    n_examples=model.n_examples,
                    n_classes=len(model.classes),
                    n_rules=len(model.rules),
                    current=version == current,
                )
            )
        return infos
