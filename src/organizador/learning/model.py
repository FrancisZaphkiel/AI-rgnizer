"""Modelo de destino: reglas explícitas + clasificador lineal calibrado con abstención.

Predicción para un archivo:

1. si aplica una regla fiable (``rules.py``), decide la regla;
2. si no, decide ``SGDClassifier(loss="log_loss")`` sobre los vectores de ``vectorize.py``;
3. las probabilidades se calibran con *temperature scaling* ajustado sobre datos que el
   clasificador no vio; por debajo de ``abstain_below`` el modelo se abstiene
   («sin clasificar») en lugar de adivinar.

El modelo nunca toca archivos: devuelve carpetas y probabilidades.
"""

from __future__ import annotations

import logging
import random
import warnings
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any, Literal

import numpy as np
from scipy.optimize import minimize_scalar
from sklearn.exceptions import ConvergenceWarning
from sklearn.linear_model import SGDClassifier

from organizador.core.config import LearningSettings
from organizador.features import FileFeatures
from organizador.learning.dataset import Dataset, Example
from organizador.learning.rules import Rule, apply_rules, learn_rules
from organizador.learning.vectorize import NAME_BLOCK, Vectorizer

log = logging.getLogger(__name__)

TOP_K = 3
CALIBRATION_FRACTION = 0.2
SEED = 20261026
MAX_REASONS = 4
# Límites de la temperatura: fuera de ellos la calibración no tiene sentido.
_T_BOUNDS = (0.05, 50.0)


class NotEnoughDataError(ValueError):
    """Hacen falta al menos dos carpetas con suficientes ejemplos."""


@dataclass(frozen=True, slots=True)
class Suggestion:
    label: str
    probability: float


@dataclass(frozen=True, slots=True)
class Prediction:
    suggestions: tuple[Suggestion, ...]
    abstained: bool
    source: Literal["regla", "modelo"]
    rule: Rule | None
    # Tokens del archivo que más empujan hacia la primera sugerencia, con su peso.
    reasons: tuple[tuple[str, float], ...]

    @property
    def best(self) -> Suggestion | None:
        return None if self.abstained else self.suggestions[0]


def _softmax(logits: np.ndarray) -> np.ndarray:
    shifted = logits - logits.max(axis=1, keepdims=True)
    exp = np.exp(shifted)
    return exp / exp.sum(axis=1, keepdims=True)  # type: ignore[no-any-return]


def _new_classifier() -> SGDClassifier:
    return SGDClassifier(
        loss="log_loss",
        alpha=1e-5,
        max_iter=50,
        tol=1e-4,
        random_state=SEED,
    )


def _class_weights(examples: Sequence[Example]) -> dict[str, float]:
    """Pesos «balanced»: una carpeta con pocos ejemplos pesa lo mismo que una grande."""
    counts: dict[str, float] = defaultdict(float)
    for e in examples:
        counts[e.label] += 1
    total, n_classes = sum(counts.values()), len(counts)
    return {label: total / (n_classes * n) for label, n in counts.items()}


def _split(examples: Sequence[Example]) -> tuple[list[Example], list[Example]]:
    """Separación estratificada: ~20 % de cada carpeta (con al menos 5) para calibrar."""
    rng = random.Random(SEED)
    by_label: dict[str, list[Example]] = defaultdict(list)
    for e in examples:
        by_label[e.label].append(e)
    train: list[Example] = []
    held: list[Example] = []
    for label in sorted(by_label):
        group = list(by_label[label])
        rng.shuffle(group)
        n_held = int(len(group) * CALIBRATION_FRACTION) if len(group) >= 5 else 0
        held.extend(group[:n_held])
        train.extend(group[n_held:])
    return train, held


class DestinationModel:
    def __init__(
        self,
        classifier: SGDClassifier,
        *,
        class_weights: dict[str, float],
        temperature: float,
        rules: dict[str, Rule],
        settings: LearningSettings,
        n_examples: int,
    ) -> None:
        self.classifier = classifier
        self.class_weights = class_weights
        self.temperature = temperature
        self.rules = rules
        self.settings = settings
        self.n_examples = n_examples
        self.trained_at = datetime.now(UTC)
        self._vectorizer = Vectorizer()

    @property
    def classes(self) -> list[str]:
        return [str(c) for c in self.classifier.classes_]

    # --- entrenamiento ------------------------------------------------------------------

    @classmethod
    def train(cls, dataset: Dataset, settings: LearningSettings) -> DestinationModel:
        examples = dataset.examples
        if len({e.label for e in examples}) < 2:
            raise NotEnoughDataError(
                "Se necesitan al menos dos carpetas con "
                f"{settings.min_examples} archivos o más para aprender"
            )
        vectorizer = Vectorizer()
        weights = _class_weights(examples)

        def fit(subset: Sequence[Example]) -> SGDClassifier:
            clf = _new_classifier()
            x = vectorizer.transform([e.features for e in subset])
            w = np.array([e.weight * weights[e.label] for e in subset])
            # Con max_iter acotado SGD avisa a menudo sin que el ajuste sea peor: se
            # evalúa con «organizador evaluate», no con este aviso.
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", ConvergenceWarning)
                clf.fit(x, [e.label for e in subset], sample_weight=w)
            return clf

        train, held = _split(examples)
        temperature = 1.0
        if held and len({e.label for e in train}) == len(weights):
            temperature = _fit_temperature(fit(train), vectorizer, held)

        classifier = fit(examples)
        classifier.sparsify()  # casi todas las columnas hasheadas son cero: ocupa mucho menos
        rules = learn_rules(
            examples,
            min_support=settings.rule_min_support,
            min_purity=settings.rule_min_purity,
        )
        log.info(
            "Modelo entrenado: %d ejemplos, %d carpetas, %d reglas, T=%.2f",
            len(examples),
            len(weights),
            len(rules),
            temperature,
        )
        return cls(
            classifier,
            class_weights=weights,
            temperature=temperature,
            rules=rules,
            settings=settings,
            n_examples=len(examples),
        )

    # --- predicción ---------------------------------------------------------------------

    def _logits(self, x: Any) -> np.ndarray:
        scores = np.asarray(self.classifier.decision_function(x), dtype=np.float64)
        if scores.ndim == 1:  # con dos clases sklearn devuelve una sola columna
            scores = np.column_stack([-scores, scores])
        return scores

    def probabilities(self, items: Sequence[FileFeatures]) -> np.ndarray:
        """Probabilidades calibradas del clasificador (sin reglas), filas = archivos."""
        return _softmax(self._logits(self._vectorizer.transform(items)) / self.temperature)

    def predict(self, features: FileFeatures) -> Prediction:
        return self.predict_many([features])[0]

    def predict_many(self, items: Sequence[FileFeatures]) -> list[Prediction]:
        if not items:
            return []
        probs = self.probabilities(items)
        classes = self.classes
        out: list[Prediction] = []
        for features, row in zip(items, probs, strict=True):
            order = np.argsort(row)[::-1]
            rule = apply_rules(self.rules, features.tokens())
            if rule is not None:
                # La regla decide; las alternativas se reparten el resto según el modelo.
                rest = [(classes[i], float(row[i])) for i in order if classes[i] != rule.label]
                mass = sum(p for _, p in rest) or 1.0
                suggestions = [Suggestion(rule.label, rule.purity)] + [
                    Suggestion(label, (1 - rule.purity) * p / mass) for label, p in rest
                ]
                source: Literal["regla", "modelo"] = "regla"
            else:
                suggestions = [Suggestion(classes[i], float(row[i])) for i in order]
                source = "modelo"
            top = tuple(suggestions[:TOP_K])
            out.append(
                Prediction(
                    suggestions=top,
                    abstained=top[0].probability < self.settings.abstain_below,
                    source=source,
                    rule=rule,
                    reasons=self.explain(features, top[0].label),
                )
            )
        return out

    def explain(self, features: FileFeatures, label: str) -> tuple[tuple[str, float], ...]:
        """Contribución de cada token del archivo hacia ``label`` (coeficiente × valor)."""
        x = self._vectorizer.transform([features])
        k = self.classes.index(label)
        coef = self.classifier.coef_
        # Con dos clases sklearn guarda un único vector: el de la clase 1.
        row, sign = (k, 1.0) if coef.shape[0] > 1 else (0, 1.0 if k == 1 else -1.0)
        cols = x.indices  # solo las columnas no nulas del archivo
        weights = coef[row, cols]
        weights = np.asarray(weights.toarray() if hasattr(weights, "toarray") else weights)
        contributions = dict(
            zip(cols.tolist(), (sign * weights.ravel() * x.data).tolist(), strict=True)
        )

        tokens = features.tokens()
        columns = self._vectorizer.token_indices(tokens)
        per_token = {t: contributions.get(columns[t], 0.0) for t in tokens}
        names = Vectorizer.name_columns()
        per_token[NAME_BLOCK] = sum(
            w for c, w in contributions.items() if names.start <= c < names.stop
        )
        positive = sorted(((t, w) for t, w in per_token.items() if w > 0), key=lambda tw: -tw[1])
        return tuple(positive[:MAX_REASONS])

    # --- aprendizaje con feedback --------------------------------------------------------

    def learn(self, features: FileFeatures, label: str) -> bool:
        """Ajuste incremental con una corrección del usuario.

        Devuelve ``False`` si ``label`` es una carpeta nueva: ``partial_fit`` no admite
        clases nuevas y hay que reentrenar el modelo completo (``train``).
        """
        if label not in self.class_weights:
            return False
        weight = self.settings.feedback_weight * self.class_weights[label]
        self.classifier.densify()
        self.classifier.partial_fit(
            self._vectorizer.transform([features]), [label], sample_weight=[weight]
        )
        self.classifier.sparsify()
        self._weaken_contradicted_rules(features, label)
        return True

    def _weaken_contradicted_rules(self, features: FileFeatures, label: str) -> None:
        """Una regla que el usuario contradice pierde pureza y, si baja del mínimo, se retira."""
        for token in features.tokens():
            rule = self.rules.get(token)
            if rule is None:
                continue
            if rule.label == label:
                updated = replace(rule, support=rule.support + 1, hits=rule.hits + 1)
            else:
                updated = replace(rule, support=rule.support + 1)
            if updated.purity >= self.settings.rule_min_purity:
                self.rules[token] = updated
            else:
                del self.rules[token]


def _fit_temperature(
    classifier: SGDClassifier, vectorizer: Vectorizer, held: Sequence[Example]
) -> float:
    """Temperatura que minimiza la log-verosimilitud negativa sobre datos no vistos."""
    scores = np.asarray(
        classifier.decision_function(vectorizer.transform([e.features for e in held]))
    )
    if scores.ndim == 1:
        scores = np.column_stack([-scores, scores])
    index = {str(c): i for i, c in enumerate(classifier.classes_)}
    targets = np.array([index[e.label] for e in held])

    def nll(log_t: float) -> float:
        probs = _softmax(scores / np.exp(log_t))
        return float(-np.log(probs[np.arange(len(targets)), targets] + 1e-12).mean())

    low, high = (float(np.log(b)) for b in _T_BOUNDS)
    result = minimize_scalar(nll, bounds=(low, high), method="bounded")
    return float(np.exp(result.x))
