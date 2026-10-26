"""Destino de un archivo combinando, por orden de prioridad:

1. **configuración**: reglas escritas por el usuario en ``config.toml``;
2. **regla**: reglas aprendidas con mucha evidencia («526 de 541 .mp3 están en Música»);
3. **modelo**: el clasificador, si su probabilidad calibrada supera el umbral;
4. **tipo**: carpeta por defecto para el tipo de archivo (imágenes → Imágenes…);
5. **sin clasificar**: el archivo se queda donde está.

Funciona sin modelo entrenado (arranque en frío): entonces solo aplican 1 y 4. Mover un
archivo con la aplicación registra la elección y el modelo aprende de ella, de modo que
lo aprendido va sustituyendo a los destinos por tipo.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from organizador.core.config import LearningSettings
from organizador.features import FileFeatures
from organizador.learning.model import DestinationModel, Prediction, Source, Suggestion


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
    items: Sequence[FileFeatures], model: DestinationModel | None, settings: LearningSettings
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
    features: FileFeatures, model: DestinationModel | None, settings: LearningSettings
) -> Prediction:
    return decide_many([features], model, settings)[0]
