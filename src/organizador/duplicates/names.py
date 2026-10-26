"""Archivos con nombres parecidos: **solo un indicio** para que el usuario revise.

Un nombre parecido no prueba nada (``informe v1.pdf`` e ``informe v2.pdf`` pueden ser
versiones que se quieren conservar), así que este módulo no propone qué conservar ni
ofrece enviar nada a la papelera: solo agrupa y puntúa.

Los nombres se normalizan (sin extensión, sufijos de copia ni ID de yt-dlp, NFKC, sin
mayúsculas, separadores ``_-.`` como espacios) y se comparan con
``rapidfuzz.fuzz.token_sort_ratio`` solo dentro de la misma extensión. Los grupos son
en estrella, como en ``similar``: cada miembro se parece al primero, sin encadenar.
Los nombres que solo difieren en sus números (series) no se consideran parecidos.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from rapidfuzz import fuzz, process
from sqlalchemy import select

from organizador.duplicates.finder import under_roots
from organizador.duplicates.keep import strip_copy_marker
from organizador.storage import Database
from organizador.storage.models import Directory, File

log = logging.getLogger(__name__)

MIN_NORMALIZED_LENGTH = 4  # «a.txt» y «b.txt» se parecen demasiado para significar algo
_ROWS_PER_CHUNK = 512  # filas de la matriz de puntuaciones calculadas a la vez
_SEPARATORS = re.compile(r"[\s._-]+")
_DIGITS = re.compile(r"\d+")
# ID de vídeo que yt-dlp añade al final: «título-yZHXoG0-f0k» o «título [FP7QnrPoORM]».
_VIDEO_ID = re.compile(r"(?:-|\s\[)(?P<id>[A-Za-z0-9_-]{11})\]?$")


def _strip_video_id(stem: str) -> str:
    match = _VIDEO_ID.search(stem)
    if match is None:
        return stem
    vid = match["id"]
    # Exige aspecto de ID (no una palabra como «Revengeance»): dígitos, «_», «-» o
    # al menos dos mayúsculas junto a minúsculas.
    looks_like_id = any(c.isdigit() or c in "_-" for c in vid) or (
        sum(c.isupper() for c in vid) >= 2 and any(c.islower() for c in vid)
    )
    return stem[: match.start()] if looks_like_id else stem


def normalize(name: str) -> str:
    stem = Path(name).stem if "." in name.lstrip(".") else name
    text = unicodedata.normalize("NFKC", strip_copy_marker(_strip_video_id(stem))).casefold()
    return _SEPARATORS.sub(" ", text).strip()


def series_key(normalized: str) -> str:
    """Nombre sin números ni espacios: «screen 20241106 232527» → «screen##»."""
    return _DIGITS.sub("#", normalized).replace(" ", "")


def is_series(a: str, b: str) -> bool:
    """¿Solo se diferencian en los números? (fechas, horas, «parte 3»/«parte 4», «v1»/«v2»).

    Son elementos distintos de una serie, no copias, así que no se proponen.
    """
    return a != b and series_key(a) == series_key(b)


@dataclass(frozen=True, slots=True)
class NameMatch:
    path: Path
    size: int
    mtime_ns: int
    # Parecido con el primer archivo del grupo (0-100; 100 = mismo nombre normalizado).
    score: int


@dataclass(frozen=True, slots=True)
class NameGroup:
    files: tuple[NameMatch, ...]

    @property
    def identical_names(self) -> bool:
        return all(f.score == 100 for f in self.files)


@dataclass(slots=True)
class NameReport:
    groups: list[NameGroup] = field(default_factory=list)
    files: int = 0


@dataclass(frozen=True, slots=True)
class _Row:
    path: Path
    size: int
    mtime_ns: int
    inode_key: tuple[int, int]
    content_hash: str | None
    normalized: str


# Margen del filtro previo entre claves de serie: «informe 2024» y «informe 2025 final»
# se parecen algo menos sin los números que con ellos.
_KEY_MARGIN = 10


def _cdist_pairs(
    rows: Sequence[str], cols: Sequence[str], min_score: int
) -> list[tuple[int, int, int]]:
    """(fila, columna, puntuación) de los pares con puntuación ≥ ``min_score``."""
    pairs: list[tuple[int, int, int]] = []
    for start in range(0, len(rows), _ROWS_PER_CHUNK):
        scores = process.cdist(
            rows[start : start + _ROWS_PER_CHUNK],
            cols,
            scorer=fuzz.token_sort_ratio,
            score_cutoff=min_score,
            dtype=np.uint8,
            workers=-1,
        )
        pairs.extend(
            (start + int(r), int(c), int(scores[r, c]))
            for r, c in zip(*np.nonzero(scores), strict=True)
        )
    return pairs


def _neighbours(names: Sequence[str], min_score: int) -> list[list[tuple[int, int]]]:
    """Para cada nombre, los (índice, puntuación) de los demás con puntuación ≥ ``min_score``.

    Las series (mismo nombre salvo los números) nunca se comparan: en una carpeta de
    capturas o de fotos numeradas casi todos los pares lo son y la comparación sería
    cuadrática. Se agrupan los nombres por clave de serie; dentro de una clave solo
    cuentan los nombres idénticos, y entre claves solo se comparan nombre a nombre las
    claves que ya se parecen entre sí.
    """
    by_key: dict[str, list[int]] = defaultdict(list)
    for i, name in enumerate(names):
        by_key[series_key(name)].append(i)
    keys = list(by_key)
    result: list[list[tuple[int, int]]] = [[] for _ in names]

    for members in by_key.values():
        same_name: dict[str, list[int]] = defaultdict(list)
        for i in members:
            same_name[names[i]].append(i)
        for group in same_name.values():
            for i in group:
                result[i].extend((j, 100) for j in group if j != i)

    for a, b, _ in _cdist_pairs(keys, keys, max(0, min_score - _KEY_MARGIN)):
        if a >= b:  # cada par de claves una sola vez, sin la diagonal
            continue
        left, right = by_key[keys[a]], by_key[keys[b]]
        for r, c, score in _cdist_pairs(
            [names[i] for i in left], [names[j] for j in right], min_score
        ):
            result[left[r]].append((right[c], score))
            result[right[c]].append((left[r], score))
    for neighbours in result:
        neighbours.sort()
    return result


def _star_groups(rows: Sequence[_Row], min_score: int) -> list[NameGroup]:
    neighbours = _neighbours([r.normalized for r in rows], min_score)
    assigned = [False] * len(rows)
    groups: list[NameGroup] = []
    for center in range(len(rows)):
        if assigned[center] or not neighbours[center]:
            continue
        members = [(center, 100)] + [(j, s) for j, s in neighbours[center] if not assigned[j]]
        if len(members) < 2:
            continue
        for j, _ in members:
            assigned[j] = True
        group_rows = [rows[j] for j, _ in members]
        # Copias exactas o hardlinks: ya los muestra la búsqueda de duplicados.
        hashes = {r.content_hash for r in group_rows}
        if len({r.inode_key for r in group_rows}) == 1 or (len(hashes) == 1 and None not in hashes):
            continue
        groups.append(
            NameGroup(
                tuple(
                    NameMatch(rows[j].path, rows[j].size, rows[j].mtime_ns, score)
                    for j, score in members
                )
            )
        )
    return groups


class SimilarNameFinder:
    def __init__(self, db: Database, *, min_score: int = 90, min_size: int = 1) -> None:
        self._db = db
        self._min_score = max(0, min(100, min_score))
        self._min_size = max(1, min_size)

    def find(self, roots: Sequence[Path]) -> NameReport:
        """Agrupa archivos indexados bajo ``roots`` por nombre parecido (no lee archivos)."""
        resolved = [r.expanduser().resolve() for r in roots]
        by_extension: dict[str, list[_Row]] = defaultdict(list)
        report = NameReport()
        with self._db.session() as session:
            rows = session.execute(
                select(
                    Directory.path,
                    File.name,
                    File.size,
                    File.mtime_ns,
                    File.device,
                    File.inode,
                    File.content_hash,
                )
                .join(Directory)
                .where(under_roots(resolved), File.size >= self._min_size)
                .order_by(Directory.path, File.name)
            )
            for dir_path, name, size, mtime, dev, ino, content in rows:
                report.files += 1
                normalized = normalize(name)
                if len(normalized) < MIN_NORMALIZED_LENGTH:
                    continue
                by_extension[Path(name).suffix.lower()].append(
                    _Row(Path(dir_path) / name, size, mtime, (dev, ino), content, normalized)
                )
        for bucket in by_extension.values():
            if len(bucket) >= 2:
                report.groups.extend(_star_groups(bucket, self._min_score))
        # Lo más fiable primero: nombres idénticos y, después, de mayor a menor parecido.
        report.groups.sort(
            key=lambda g: (
                not g.identical_names,
                -min(f.score for f in g.files),
                str(g.files[0].path),
            )
        )
        log.info("Nombres parecidos en %s: %d grupos", resolved, len(report.groups))
        return report
