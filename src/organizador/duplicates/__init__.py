"""Detección de duplicados exactos: hashing por etapas, agrupación y espacio recuperable."""

from organizador.duplicates.cleanup import trash_redundant
from organizador.duplicates.finder import (
    DuplicateFile,
    DuplicateFinder,
    DuplicateGroup,
    DuplicateReport,
    DuplicateStats,
)
from organizador.duplicates.keep import KeepPolicy

__all__ = [
    "DuplicateFile",
    "DuplicateFinder",
    "DuplicateGroup",
    "DuplicateReport",
    "DuplicateStats",
    "KeepPolicy",
    "trash_redundant",
]
