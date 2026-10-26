"""Evaluación offline: ¿acierta el modelo con archivos que no vio al entrenar?

Se reserva una parte estratificada de los archivos organizados (nunca las correcciones
del usuario), se entrena con el resto y se mide sobre la parte reservada:

- **cobertura**: fracción de archivos para los que no se abstiene;
- **top-1 / top-3** sobre las predicciones no abstenidas (los objetivos de la Fase 3);
- **calibración** (ECE y curva de fiabilidad): si dice 80 %, ¿acierta el 80 % de las veces?
  Es requisito antes de permitir el modo automático.
"""

from __future__ import annotations

import random
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field

from airgnizer.core.config import LearningSettings
from airgnizer.learning.dataset import Dataset, Example
from airgnizer.learning.model import DestinationModel, Prediction

TEST_FRACTION = 0.2
CALIBRATION_BINS = 10
SEED = 7


@dataclass(frozen=True, slots=True)
class ReliabilityBin:
    low: float
    high: float
    count: int
    confidence: float
    accuracy: float


@dataclass(slots=True)
class Evaluation:
    n_train: int = 0
    n_test: int = 0
    n_classes: int = 0
    covered: int = 0
    top1: int = 0
    top3: int = 0
    top1_all: int = 0
    by_rule: int = 0
    by_rule_correct: int = 0
    ece: float = 0.0
    bins: list[ReliabilityBin] = field(default_factory=list)

    @property
    def coverage(self) -> float:
        return self.covered / self.n_test if self.n_test else 0.0

    @property
    def abstention(self) -> float:
        return 1 - self.coverage

    @property
    def top1_accuracy(self) -> float:
        return self.top1 / self.covered if self.covered else 0.0

    @property
    def top3_accuracy(self) -> float:
        return self.top3 / self.covered if self.covered else 0.0


def split_for_evaluation(examples: Sequence[Example]) -> tuple[list[Example], list[Example]]:
    rng = random.Random(SEED)
    by_label: dict[str, list[Example]] = defaultdict(list)
    for e in examples:
        by_label[e.label].append(e)
    train: list[Example] = []
    test: list[Example] = []
    for label in sorted(by_label):
        group = by_label[label]
        organized = [e for e in group if e.weight == 1.0]
        feedback = [e for e in group if e.weight != 1.0]
        rng.shuffle(organized)
        n_test = int(len(organized) * TEST_FRACTION)
        test.extend(organized[:n_test])
        train.extend(organized[n_test:] + feedback)
    return train, test


def _reliability(
    confidences: list[float], correct: list[bool]
) -> tuple[float, list[ReliabilityBin]]:
    bins: list[ReliabilityBin] = []
    ece = 0.0
    n = len(confidences)
    for b in range(CALIBRATION_BINS):
        low, high = b / CALIBRATION_BINS, (b + 1) / CALIBRATION_BINS
        idx = [
            i
            for i, c in enumerate(confidences)
            if low <= c < high or (b == CALIBRATION_BINS - 1 and c == 1.0)
        ]
        if not idx:
            continue
        conf = sum(confidences[i] for i in idx) / len(idx)
        acc = sum(correct[i] for i in idx) / len(idx)
        ece += len(idx) / n * abs(conf - acc)
        bins.append(ReliabilityBin(low, high, len(idx), conf, acc))
    return ece, bins


def evaluate(dataset: Dataset, settings: LearningSettings) -> Evaluation:
    train, test = split_for_evaluation(dataset.examples)
    model = DestinationModel.train(Dataset(train, dataset.stats), settings)
    known = set(model.classes)
    test = [e for e in test if e.label in known]
    predictions: list[Prediction] = model.predict_many([e.features for e in test])

    result = Evaluation(n_train=len(train), n_test=len(test), n_classes=len(known))
    confidences: list[float] = []
    correct: list[bool] = []
    for example, prediction in zip(test, predictions, strict=True):
        labels = [s.label for s in prediction.suggestions]
        hit = labels[0] == example.label
        result.top1_all += hit
        confidences.append(prediction.suggestions[0].probability)
        correct.append(hit)
        if prediction.abstained:
            continue
        result.covered += 1
        result.top1 += hit
        result.top3 += example.label in labels
        if prediction.source == "regla":
            result.by_rule += 1
            result.by_rule_correct += hit
    result.ece, result.bins = _reliability(confidences, correct)
    return result
