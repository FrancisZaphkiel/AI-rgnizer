"""Reglas explícitas «token → carpeta», aprendidas de los propios datos.

Una regla existe cuando casi todos los archivos con un token están en una misma carpeta
(«el 97 % de tus .mp3 están en Música»). Tienen prioridad sobre el modelo porque son
fáciles de explicar y de corregir, y funcionan con pocos datos (arranque en frío).

Solo se consideran tokens que describen el tipo o el origen del archivo; las palabras
del nombre se dejan al modelo, que las combina con el resto.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from airgnizer.learning.dataset import Example

RULE_PREFIXES = (
    "ext:",
    "origen:",
    "pdf.autor:",
    "pdf.creador:",
    "pdf.productor:",
    "exif.marca:",
    "exif.modelo:",
    "audio.artista:",
    "audio.album:",
)


@dataclass(frozen=True, slots=True)
class Rule:
    token: str
    label: str
    # Ejemplos con el token y, de ellos, los que están en ``label``.
    support: int
    hits: int

    @property
    def purity(self) -> float:
        """Fracción suavizada (+1 en el denominador): conservadora con poco soporte."""
        return self.hits / (self.support + 1)


def learn_rules(
    examples: Iterable[Example], *, min_support: int, min_purity: float
) -> dict[str, Rule]:
    counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for example in examples:
        for token in example.features.tokens():
            if token.startswith(RULE_PREFIXES):
                counts[token][example.label] += 1
    rules: dict[str, Rule] = {}
    for token, by_label in counts.items():
        support = sum(by_label.values())
        label, hits = max(by_label.items(), key=lambda kv: (kv[1], kv[0]))
        rule = Rule(token, label, support, hits)
        if support >= min_support and rule.purity >= min_purity:
            rules[token] = rule
    return rules


def apply_rules(rules: Mapping[str, Rule], tokens: Sequence[str]) -> Rule | None:
    """La regla más fiable que aplica, salvo que dos reglas apunten a carpetas distintas.

    Ante un conflicto (un .mp3 con el autor de los PDF del trabajo) no decide ninguna
    regla: decide el modelo, que pondera todas las señales.
    """
    matching = [rules[t] for t in tokens if t in rules]
    if not matching or len({r.label for r in matching}) > 1:
        return None
    return max(matching, key=lambda r: (r.purity, r.support))
