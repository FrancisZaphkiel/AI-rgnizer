"""Duplicados exactos (hashing por etapas, espacio recuperable) e imágenes similares (pHash)."""

from airgnizer.duplicates.cleanup import trash_redundant, trash_similar
from airgnizer.duplicates.finder import (
    DuplicateFile,
    DuplicateFinder,
    DuplicateGroup,
    DuplicateReport,
    DuplicateStats,
)
from airgnizer.duplicates.keep import KeepPolicy
from airgnizer.duplicates.similar import (
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
