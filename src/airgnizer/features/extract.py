"""Características de un archivo para el motor de sugerencias.

``extract_features`` lee el archivo (solo lectura) y devuelve un ``FileFeatures``;
``FileFeatures.tokens()`` lo convierte en tokens legibles («ext:pdf», «pal:cartola»,
«pdf.productor:bancoestado») que sirven a la vez de entrada al modelo y de explicación
para el usuario («se parece a lo que guardas en Banco por: pal:cartola, ext:pdf»).

Cambiar lo que se extrae o cómo se tokeniza exige subir ``FEATURES_VERSION``: las
características guardadas en el índice con otra versión se recalculan.
"""

from __future__ import annotations

import json
import mimetypes
import re
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from airgnizer.features.metadata import extract_metadata, mime_type, origin_domain
from airgnizer.features.name import NameDate, NameInfo, analyse_name, fold

FEATURES_VERSION = 1

# Límites en bytes de los tramos de tamaño (cada token cubre un orden de magnitud).
_SIZE_BUCKETS = (
    (10 * 1024, "<10K"),
    (100 * 1024, "<100K"),
    (1024**2, "<1M"),
    (10 * 1024**2, "<10M"),
    (100 * 1024**2, "<100M"),
    (1024**3, "<1G"),
)
_MAX_TEXT_WORDS = 15
_TEXT_WORD = re.compile(r"[^\W\d_]{4,}")
# Palabras demasiado frecuentes para distinguir nada (español e inglés).
_STOPWORDS = frozenset(
    {
        "para",
        "como",
        "pero",
        "este",
        "esta",
        "estos",
        "estas",
        "desde",
        "entre",
        "sobre",
        "todo",
        "todos",
        "todas",
        "cada",
        "donde",
        "cuando",
        "porque",
        "segun",
        "sino",
        "tiene",
        "tienen",
        "sera",
        "puede",
        "pueden",
        "tambien",
        "otro",
        "otra",
        "otros",
        "otras",
        "mismo",
        "misma",
        "hasta",
        "hacia",
        "durante",
        "mediante",
        "that",
        "this",
        "with",
        "from",
        "have",
        "will",
        "your",
        "their",
        "which",
        "there",
        "been",
        "were",
        "would",
        "about",
        "into",
        "more",
        "than",
        "they",
        "them",
        "what",
        "when",
        "only",
        "also",
        "page",
        "pagina",
    }
)
# Claves de metadatos que se convierten en tokens (el resto, como el texto, se procesa aparte).
_META_TOKEN_KEYS = {
    "exif": ("marca", "modelo", "software", "año_captura"),
    "pdf": ("autor", "creador", "productor", "cifrado"),
    "audio": ("artista", "album", "genero"),
}


def size_bucket(size: int) -> str:
    for limit, label in _SIZE_BUCKETS:
        if size < limit:
            return label
    return ">=1G"


def _token_value(value: str, max_length: int = 40) -> str:
    return "_".join(fold(value).split())[:max_length]


@dataclass(frozen=True, slots=True)
class FileFeatures:
    name: NameInfo
    size: int
    mtime_ns: int
    mime: str | None
    origin: str | None
    metadata: dict[str, dict[str, str]] = field(default_factory=dict)

    def tokens(self) -> list[str]:
        """Tokens sin repetir, en orden estable."""
        out: list[str] = []
        name = self.name
        if name.extension:
            out.append(f"ext:{name.extension}")
        if self.mime:
            out.append(f"mime:{self.mime}")
            out.append(f"tipo:{self.mime.split('/', 1)[0]}")
        out.append(f"tamaño:{size_bucket(self.size)}")
        out.extend(f"pal:{w}" for w in name.words)
        if name.date is not None:
            out.append(f"fecha:{name.date.format}")
            out.append(f"año:{name.date.year}")
        if name.version is not None:
            out.append("versión")
        if name.copy_marker:
            out.append("copia")
        modified = datetime.fromtimestamp(self.mtime_ns / 1e9, tz=UTC)
        out.append(f"modificado:{modified.year}")
        if self.origin:
            out.append(f"origen:{self.origin}")
        for kind, keys in _META_TOKEN_KEYS.items():
            meta = self.metadata.get(kind, {})
            out.extend(f"{kind}.{k}:{_token_value(meta[k])}" for k in keys if meta.get(k))
        out.extend(f"texto:{w}" for w in self.text_keywords())
        return list(dict.fromkeys(out))

    def text_keywords(self) -> list[str]:
        """Palabras frecuentes de la primera página de un PDF (si tiene texto)."""
        text = self.metadata.get("pdf", {}).get("texto", "")
        counts: dict[str, int] = {}
        for word in _TEXT_WORD.findall(text):
            folded = fold(word)
            if folded not in _STOPWORDS:
                counts[folded] = counts.get(folded, 0) + 1
        ranked = sorted(counts, key=lambda w: (-counts[w], w))
        return ranked[:_MAX_TEXT_WORDS]

    # --- persistencia en el índice ------------------------------------------------------

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, separators=(",", ":"))

    @classmethod
    def from_json(cls, raw: str) -> FileFeatures:
        data: dict[str, Any] = json.loads(raw)
        name = dict(data["name"])
        date = name.pop("date")
        name["words"] = tuple(name["words"])
        return cls(
            name=NameInfo(**name, date=NameDate(**date) if date else None),
            size=data["size"],
            mtime_ns=data["mtime_ns"],
            mime=data["mime"],
            origin=data["origin"],
            metadata=data["metadata"],
        )


def extract_features(path: Path, *, with_metadata: bool = True) -> FileFeatures:
    """Características de ``path``. Solo lanza ``OSError`` si el archivo no se puede leer.

    Con ``with_metadata=False`` se omite todo lo que abre el archivo (MIME por
    contenido, EXIF, PDF, audio): solo nombre, tamaño, fechas, MIME por extensión y origen.
    """
    st = path.stat()
    mime = mime_type(path) if with_metadata else None
    if mime is None:
        mime = mimetypes.guess_type(path.name, strict=False)[0]
    return FileFeatures(
        name=analyse_name(path.name),
        size=st.st_size,
        mtime_ns=st.st_mtime_ns,
        mime=mime,
        origin=origin_domain(path),
        metadata=extract_metadata(path, mime) if with_metadata else {},
    )
