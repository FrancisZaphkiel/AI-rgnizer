"""De ``FileFeatures`` a vectores dispersos para el clasificador.

Se usa *hashing* en lugar de un vocabulario aprendido (``TfidfVectorizer``): el espacio
de características es fijo, así que un archivo con palabras nunca vistas se vectoriza
igual y el modelo admite aprendizaje incremental (``partial_fit``).

Dos bloques, normalizados por separado para que ninguno domine:

1. tokens de ``FileFeatures.tokens()`` (extensión, MIME, palabras, metadatos…);
2. n-gramas de caracteres del nombre: capturan parecidos que las palabras no ven
   («cartola» ~ «cartolas», «Calidad_Soft» ~ «calidad de software»).
"""

from __future__ import annotations

import re
from collections.abc import Sequence

import numpy as np
from scipy import sparse
from sklearn.feature_extraction import FeatureHasher
from sklearn.feature_extraction.text import HashingVectorizer
from sklearn.preprocessing import normalize

from organizador.duplicates.keep import strip_copy_marker
from organizador.features import FileFeatures
from organizador.features.name import fold, strip_video_id

TOKEN_FEATURES = 2**18
NAME_FEATURES = 2**18
_SEPARATORS = re.compile(r"[\s._\-()\[\]]+")
_DIGIT = re.compile(r"\d")

# Prefijo del bloque de n-gramas en las explicaciones.
NAME_BLOCK = "nombre"


def name_text(features: FileFeatures) -> str:
    """Nombre normalizado para los n-gramas: sin tildes, separadores ni dígitos concretos."""
    stem = strip_copy_marker(strip_video_id(features.name.stem))
    text = _SEPARATORS.sub(" ", fold(stem)).strip()
    return _DIGIT.sub("0", text)  # «2024-05» y «2023-11» comparten forma


class Vectorizer:
    """Sin estado aprendido: dos instancias producen siempre los mismos vectores."""

    def __init__(self) -> None:
        self._tokens = FeatureHasher(
            n_features=TOKEN_FEATURES, input_type="string", alternate_sign=False
        )
        self._names = HashingVectorizer(
            analyzer="char_wb",
            ngram_range=(3, 5),
            n_features=NAME_FEATURES,
            alternate_sign=False,
            lowercase=False,
            norm="l2",
        )

    @property
    def n_features(self) -> int:
        return TOKEN_FEATURES + NAME_FEATURES

    def transform(self, items: Sequence[FileFeatures]) -> sparse.csr_matrix:
        tokens = normalize(self._tokens.transform(f.tokens() for f in items))
        names = self._names.transform([name_text(f) for f in items])
        return sparse.hstack([tokens, names], format="csr", dtype=np.float64)

    def token_indices(self, tokens: Sequence[str]) -> dict[str, int]:
        """Columna de cada token en la matriz (para explicar una predicción)."""
        hashed = self._tokens.transform([[t] for t in tokens])
        return {t: int(hashed.indices[hashed.indptr[i]]) for i, t in enumerate(tokens)}

    @staticmethod
    def name_columns() -> slice:
        return slice(TOKEN_FEATURES, TOKEN_FEATURES + NAME_FEATURES)
