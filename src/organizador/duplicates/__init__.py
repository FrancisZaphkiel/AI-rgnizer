"""Duplicados exactos (hashing por etapas, espacio recuperable) e imágenes similares (pHash)."""

from organizador.duplicates.cleanup import trash_redundant, trash_similar
from organizador.duplicates.finder import (
    DuplicateFile,
    DuplicateFinder,
    DuplicateGroup,
    DuplicateReport,
    DuplicateStats,
)
from organizador.duplicates.keep import KeepPolicy
from organizador.duplicates.similar import (
    ConfirmedSimilarGroup,
    SimilarFile,
    SimilarGroup,
    SimilarImageFinder,
    SimilarReport,
)

__all__ = [
    "ConfirmedSimilarGroup",
    "DuplicateFile",
    "DuplicateFinder",
    "DuplicateGroup",
    "DuplicateReport",
    "DuplicateStats",
    "KeepPolicy",
    "SimilarFile",
    "SimilarGroup",
    "SimilarImageFinder",
    "SimilarReport",
    "trash_redundant",
    "trash_similar",
]
