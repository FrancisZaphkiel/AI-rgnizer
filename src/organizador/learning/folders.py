"""Coincidencias entre el nombre de un archivo y el nombre de una carpeta existente.

``Salud_receta.pdf`` apunta a ``Documentos/Salud`` aunque esa carpeta tenga pocos
archivos y el modelo no la conozca: las carpetas también se describen por su nombre.
Solo cuenta si **todas** las palabras significativas del nombre de la carpeta aparecen
en el nombre del archivo, y nunca si dos carpetas distintas empatan (ambigüedad).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select

from organizador.core.paths import FolderSet
from organizador.features.name import analyse_name
from organizador.storage import Database
from organizador.storage.index import under_roots
from organizador.storage.models import Directory

# Palabras más cortas («doc», «pdf», «2024») coinciden por casualidad.
MIN_FOLDER_WORD = 4


@dataclass(frozen=True, slots=True)
class FolderMatch:
    folder: str
    words: tuple[str, ...]


def folder_words(name: str) -> frozenset[str]:
    words = analyse_name(name).words
    return frozenset(w for w in words if len(w) >= MIN_FOLDER_WORD and not w.isdigit())


class FolderNames:
    def __init__(self, folders: Iterable[str]) -> None:
        self._folders = [(f, words) for f in folders if (words := folder_words(Path(f).name))]

    def __len__(self) -> int:
        return len(self._folders)

    @classmethod
    def from_index(
        cls, db: Database, roots: Sequence[Path], *, exclude: Sequence[Path] = ()
    ) -> FolderNames:
        """Carpetas indexadas bajo ``roots``, salvo las raíces y lo que cuelga de ``exclude``."""
        resolved = [r.expanduser().resolve() for r in roots]
        top = {str(r) for r in resolved}
        skipped = FolderSet(e.expanduser().resolve() for e in exclude)
        with db.session() as session:
            dirs = session.scalars(select(Directory.path).where(under_roots(resolved))).all()
        return cls(d for d in dirs if d not in top and d not in skipped)

    def match(self, file_words: Iterable[str]) -> FolderMatch | None:
        present = set(file_words)
        best: list[tuple[str, frozenset[str]]] = []
        for folder, words in self._folders:
            if not words <= present:
                continue
            if not best or len(words) > len(best[0][1]):
                best = [(folder, words)]
            elif len(words) == len(best[0][1]):
                best.append((folder, words))
        if len(best) != 1:
            return None
        folder, words = best[0]
        return FolderMatch(folder, tuple(sorted(words)))
