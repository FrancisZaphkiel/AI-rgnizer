"""Destino de un archivo combinando, por orden de prioridad:

1. **configuración**: reglas escritas por el usuario en ``config.toml``;
2. **regla**: reglas aprendidas con mucha evidencia («526 de 541 .mp3 están en Música»);
3. **modelo**: el clasificador, si su probabilidad calibrada supera el umbral;
4. **carpeta**: una carpeta existente cuyo nombre aparece en el del archivo
   (``Salud_receta.pdf`` → ``Salud``; ver ``folders.py``);
5. **tipo**: carpeta por defecto para el tipo de archivo (imágenes → Imágenes…);
6. **sin clasificar**: el archivo se queda donde está.

Funciona sin modelo entrenado (arranque en frío): entonces aplican 1, 4 y 5. Mover un
archivo con la aplicación registra la elección y el modelo aprende de ella, de modo que
lo aprendido va sustituyendo a los destinos por tipo.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from airgnizer.core.config import LearningSettings
from airgnizer.features import FileFeatures
from airgnizer.learning.folders import FolderNames
from airgnizer.learning.model import DestinationModel, Prediction, Source, Suggestion


def _folder(raw: str) -> str:
    return str(Path(raw).expanduser())


def _first_match(table: dict[str, str], tokens: Sequence[str]) -> tuple[str, str] | None:
    """Primera entrada de ``table`` (en su orden) cuyo token tiene el archivo."""
    present = set(tokens)
    for token, folder in table.items():
        if token in present:
            return token, _folder(folder)
    return None


def _fixed(
    label: str, token: str, source: Source, probability: float, model_prediction: Prediction | None
) -> Prediction:
    """Destino decidido fuera del modelo; sus sugerencias quedan como alternativas."""
    alternatives = [] if model_prediction is None else list(model_prediction.suggestions)
    rest = [s for s in alternatives if s.label != label]
    return Prediction(
        suggestions=(Suggestion(label, probability), *rest[:2]),
        abstained=False,
        source=source,
        rule=None,
        reasons=((token, 1.0),),
    )


def decide_many(
    items: Sequence[FileFeatures],
    model: DestinationModel | None,
    settings: LearningSettings,
    folders: FolderNames | None = None,
) -> list[Prediction]:
    predictions: list[Prediction | None] = (
        list(model.predict_many(items)) if model is not None else [None] * len(items)
    )
    out: list[Prediction] = []
    for features, predicted in zip(items, predictions, strict=True):
        tokens = features.tokens()
        if (match := _first_match(settings.rules, tokens)) is not None:
            token, folder = match
            out.append(_fixed(folder, token, "configuración", 1.0, predicted))
        elif predicted is not None and not predicted.abstained:
            out.append(predicted)  # regla aprendida o modelo seguro
        elif (
            folders is not None
            and (found := folders.match(t[4:] for t in tokens if t.startswith("pal:")))
            # «SalaU_figma.zip» junto a la carpeta «SalaU_figma» (o «informe.docx» junto a
            # «informe.docx_artifacts») es el original de lo extraído, no va dentro.
            and not Path(found.folder).name.startswith(features.name.stem)
        ):
            reason = "pal:" + ", pal:".join(found.words)
            out.append(_fixed(found.folder, reason, "carpeta", 0.0, predicted))
        elif (match := _first_match(settings.type_destinations, tokens)) is not None:
            token, folder = match
            out.append(_fixed(folder, token, "tipo", 0.0, predicted))
        elif predicted is not None:
            out.append(predicted)  # abstención: el modelo ofrece alternativas
        else:
            out.append(
                Prediction(suggestions=(), abstained=True, source="modelo", rule=None, reasons=())
            )
    return out


def decide(
    features: FileFeatures,
    model: DestinationModel | None,
    settings: LearningSettings,
    folders: FolderNames | None = None,
) -> Prediction:
    return decide_many([features], model, settings, folders)[0]
