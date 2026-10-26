"""Archivos posiblemente mal ubicados fuera de las carpetas vigiladas.

Un archivo que ya está en una carpeta indexada es, a la vez, ejemplo de entrenamiento:
un modelo que lo vio diría que está bien donde está. Por eso se usa validación cruzada
(``FOLDS`` partes): cada archivo se juzga con un modelo entrenado sin él.

Solo se proponen cambios con evidencia fuerte: una regla de ``config.toml``, el nombre de
una carpeta existente, o el modelo/una regla aprendida con al menos
``review_min_confidence``. El destino por tipo nunca basta para proponer mover algo.

No se revisan:

- archivos que forman parte de una estructura (una web exportada, los recursos de una
  presentación): a más de ``MAX_NESTING`` niveles bajo su carpeta, o cuyo nombre sin
  números ni hashes (``image_000003_<hash>.png`` → ``png:image``) se repite en
  ``PATTERN_DIRS`` carpetas o más. Moverlos por separado rompería el conjunto;
- archivos con el mismo nombre en dos carpetas: el usuario guardó una copia en cada una
  a propósito, y cada copia «apuntaría» a la otra.
"""

from __future__ import annotations

import random
import re
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from organizador.core.config import LearningSettings
from organizador.learning.dataset import Dataset, Example
from organizador.learning.decide import decide_many
from organizador.learning.folders import FolderNames
from organizador.learning.model import DestinationModel, NotEnoughDataError, Prediction

FOLDS = 5
SEED = 11
MAX_NESTING = 1
PATTERN_DIRS = 3
_HASHLIKE = re.compile(r"[0-9a-f]{8,}")
_NON_LETTERS = re.compile(r"[^\w]|[\d_]")
# Una sola palabra en común con una carpeta («prueba») no basta para mover algo que el
# usuario ya organizó; para sugerir destino a un archivo nuevo sí (``Salud_receta.pdf``).
MIN_FOLDER_WORDS = 2


@dataclass(frozen=True, slots=True)
class Misplaced:
    path: Path
    current: str
    prediction: Prediction

    @property
    def target(self) -> str:
        best = self.prediction.best
        assert best is not None  # solo se crean con una sugerencia firme
        return best.label


@dataclass(frozen=True, slots=True)
class ReviewReport:
    misplaced: list[Misplaced]
    reviewed: int


def is_in(path: Path, folder: str) -> bool:
    """¿Está ``path`` dentro de ``folder`` (a cualquier profundidad)?"""
    return path.parent.is_relative_to(folder)


def _is_strong(prediction: Prediction, settings: LearningSettings) -> bool:
    best = prediction.best
    if best is None or prediction.source == "tipo":
        return False
    if prediction.source == "configuración":
        return True
    if prediction.source == "carpeta":
        return len(prediction.reasons[0][0].split(",")) >= MIN_FOLDER_WORDS
    return best.probability >= settings.review_min_confidence


def _pattern(example: Example) -> str:
    name = example.features.name
    letters = _NON_LETTERS.sub(" ", _HASHLIKE.sub(" ", name.stem.casefold())).split()
    return f"{name.extension}:{' '.join(letters)}"


def _reviewable(examples: Sequence[Example]) -> list[Example]:
    """Archivos organizados que se pueden juzgar por separado (ver docstring del módulo)."""
    dirs_by_name: dict[str, set[Path]] = defaultdict(set)
    dirs_by_pattern: dict[str, set[Path]] = defaultdict(set)
    for e in examples:
        dirs_by_name[e.path.name].add(e.path.parent)
        dirs_by_pattern[_pattern(e)].add(e.path.parent)
    return [
        e
        for e in examples
        if e.weight == 1.0
        and len(e.path.parent.relative_to(e.label).parts) <= MAX_NESTING
        and len(dirs_by_name[e.path.name]) == 1
        and len(dirs_by_pattern[_pattern(e)]) < PATTERN_DIRS
    ]


def _folds(examples: list[Example]) -> list[list[Example]]:
    organized = _reviewable(examples)
    random.Random(SEED).shuffle(organized)
    return [organized[i::FOLDS] for i in range(FOLDS)]


def find_misplaced(
    dataset: Dataset,
    settings: LearningSettings,
    folders: FolderNames | None = None,
    *,
    progress: Callable[[int, int], None] | None = None,
) -> ReviewReport:
    found: list[Misplaced] = []
    parts = _folds(dataset.examples)
    for index, held in enumerate(parts, start=1):
        if progress is not None:
            progress(index, len(parts))
        held_ids = {id(e) for e in held}
        train = [e for e in dataset.examples if id(e) not in held_ids]
        try:
            model: DestinationModel | None = DestinationModel.train(
                Dataset(train, dataset.stats), settings
            )
        except NotEnoughDataError:
            model = None  # aún pueden aplicar las reglas de config y los nombres de carpeta
        predictions = decide_many([e.features for e in held], model, settings, folders)
        for example, prediction in zip(held, predictions, strict=True):
            if not _is_strong(prediction, settings):
                continue
            best = prediction.best
            if best is not None and not is_in(example.path, best.label):
                found.append(Misplaced(example.path, example.label, prediction))
    found.sort(
        key=lambda m: (
            m.prediction.source != "configuración",
            -m.prediction.suggestions[0].probability,
        )
    )
    return ReviewReport(found, sum(len(p) for p in parts))
