from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

from organizador.core.config import LearningSettings
from organizador.features import FileFeatures
from organizador.learning.decide import decide
from organizador.learning.model import Prediction, Suggestion
from organizador.learning.rules import Rule


class _Features:
    def __init__(self, *tokens: str) -> None:
        self._tokens = list(tokens)

    def tokens(self) -> list[str]:
        return self._tokens


class _Model:
    def __init__(self, prediction: Prediction) -> None:
        self.prediction = prediction

    def predict_many(self, items: Any) -> list[Prediction]:
        return [self.prediction for _ in items]


def _pred(label: str, p: float, source: Any = "modelo", *, abstained: bool = False) -> Prediction:
    rule = Rule("ext:jpg", label, 100, 99) if source == "regla" else None
    return Prediction((Suggestion(label, p), Suggestion("/otra", 0.1)), abstained, source, rule, ())


SETTINGS = LearningSettings(
    rules={"ext:jpg": "~/Fotos"}, type_destinations={"tipo:image": "/Imágenes"}
)
JPG: Any = _Features("ext:jpg", "tipo:image")


def _decide(features: Any, prediction: Prediction | None, settings: LearningSettings) -> Prediction:
    model: Any = None if prediction is None else _Model(prediction)
    return decide(features, model, settings)


def test_config_rule_first() -> None:
    result = _decide(JPG, _pred("/aprendido", 0.99, "regla"), SETTINGS)
    assert result.source == "configuración"
    assert result.best == Suggestion(str(Path("~/Fotos").expanduser()), 1.0)
    assert [s.label for s in result.suggestions[1:]] == ["/aprendido", "/otra"]


def test_learned_rule_then_model() -> None:
    no_rules = replace(SETTINGS, rules={})
    assert _decide(JPG, _pred("/aprendido", 0.99, "regla"), no_rules).source == "regla"
    assert _decide(JPG, _pred("/modelo", 0.8), no_rules).source == "modelo"


def test_type_when_model_unsure_or_missing() -> None:
    no_rules = replace(SETTINGS, rules={})
    unsure = _decide(JPG, _pred("/modelo", 0.3, abstained=True), no_rules)
    assert unsure.source == "tipo" and unsure.best is not None
    assert unsure.best.label == "/Imágenes"
    assert _decide(JPG, None, no_rules).source == "tipo"


def test_unclassified_last() -> None:
    bare = LearningSettings(rules={}, type_destinations={})
    other: FileFeatures = _Features("ext:xyz")  # type: ignore[assignment]
    assert _decide(other, _pred("/m", 0.2, abstained=True), bare).abstained
    nothing = _decide(other, None, bare)
    assert nothing.abstained and nothing.best is None and nothing.suggestions == ()


def test_default_type_destinations_use_xdg_dirs(isolated_home: Path) -> None:
    defaults = LearningSettings().type_destinations
    assert defaults["tipo:image"] == str(isolated_home / "Pictures")
    assert defaults["ext:pdf"] == str(isolated_home / "Documents")
