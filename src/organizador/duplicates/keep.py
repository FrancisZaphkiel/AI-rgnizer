"""Heurística para proponer qué copia de un grupo conservar.

Es solo una recomendación: el usuario siempre puede elegir otra. Criterios, en orden:

1. dentro de una carpeta organizada (indexada y no vigilada);
2. fuera de las carpetas vigiladas (Descargas);
3. nombre sin sufijo de copia: ``(1)``, ``copia``, ``copy``, ``Copia de …``;
4. la creada antes (``btime``): ``mtime`` no sirve para esto, porque Dolphin y ``cp -p``
   la conservan al copiar; si el sistema de archivos no guarda ``btime``, se usa ``mtime``;
5. la modificada antes (``mtime``);
6. desempate determinista: ruta más corta y, después, orden alfabético.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

_COPY_SUFFIX = re.compile(
    r"""(?<=\S)(?:
        \s*\(\d+\)                                    # «foto (1)»
      | [\s_-]+(?:copia|copy)(?:[\s_-]*\(?\d+\)?)?     # «foto - copia», «foto_copy 2»
    )$""",
    re.IGNORECASE | re.VERBOSE,
)
_COPY_PREFIX = re.compile(r"^(?:copia de|copy of)\s+", re.IGNORECASE)


def has_copy_marker(name: str) -> bool:
    """¿El nombre parece el de una copia? Se ignora la extensión."""
    stem = Path(name).stem if "." in name.lstrip(".") else name
    return bool(_COPY_SUFFIX.search(stem) or _COPY_PREFIX.match(stem))


class KeepCandidate(Protocol):
    @property
    def path(self) -> Path: ...

    @property
    def mtime_ns(self) -> int: ...

    @property
    def birth_ns(self) -> int | None: ...


@dataclass(frozen=True, slots=True)
class KeepChoice[T: KeepCandidate]:
    keep: T
    reason: str


def _is_under(path: Path, dirs: Iterable[Path]) -> bool:
    return any(path.is_relative_to(d) for d in dirs)


class KeepPolicy:
    def __init__(self, organized_dirs: Sequence[Path], watched_dirs: Sequence[Path]) -> None:
        self._organized = tuple(d.expanduser().absolute() for d in organized_dirs)
        self._watched = tuple(d.expanduser().absolute() for d in watched_dirs)

    def _criteria(self, c: KeepCandidate) -> tuple[tuple[bool | int | str, str], ...]:
        """Pares (valor, motivo); se ordena de menor a mayor valor."""
        watched = _is_under(c.path, self._watched)
        organized = not watched and _is_under(c.path, self._organized)
        return (
            (not organized, "está en una carpeta organizada"),
            (watched, "está fuera de las carpetas vigiladas"),
            (has_copy_marker(c.path.name), "su nombre no indica que sea una copia"),
            (
                c.birth_ns if c.birth_ns is not None else c.mtime_ns,
                "se creó antes que las demás",
            ),
            (c.mtime_ns, "es la modificada hace más tiempo"),
            (len(str(c.path)), "tiene la ruta más corta"),
            (str(c.path), "orden alfabético"),
        )

    def choose[T: KeepCandidate](self, candidates: Sequence[T]) -> KeepChoice[T]:
        if not candidates:
            raise ValueError("Se necesita al menos un candidato")
        ranked = sorted(candidates, key=lambda c: [v for v, _ in self._criteria(c)])
        best = ranked[0]
        if len(ranked) == 1:
            return KeepChoice(best, "única copia")
        # El motivo es el primer criterio que distingue a la mejor de la segunda.
        for (best_value, reason), (other_value, _) in zip(
            self._criteria(best), self._criteria(ranked[1]), strict=True
        ):
            if best_value != other_value:
                return KeepChoice(best, reason)
        return KeepChoice(best, "orden alfabético")  # pragma: no cover - rutas únicas
