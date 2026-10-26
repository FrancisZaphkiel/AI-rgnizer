"""Análisis del nombre de un archivo: palabras, fechas, versiones y marcas de copia.

Es la señal más importante para predecir el destino («cartola_2024-05.pdf» → Banco) y
la única que siempre existe.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from airgnizer.duplicates.keep import has_copy_marker, strip_copy_marker

# Fechas en el nombre, de más a menos específicas. Se exige separación de otros dígitos
# para no leer una fecha dentro de un número largo (un ID, un timestamp en ms).
_DATE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "AAAA-MM-DD",
        re.compile(r"(?<!\d)(?P<y>(?:19|20)\d\d)[-_.](?P<m>[01]\d)[-_.](?P<d>[0-3]\d)(?!\d)"),
    ),
    (
        "DD-MM-AAAA",
        re.compile(r"(?<!\d)(?P<d>[0-3]\d)[-_.](?P<m>[01]\d)[-_.](?P<y>(?:19|20)\d\d)(?!\d)"),
    ),
    ("AAAAMMDD", re.compile(r"(?<!\d)(?P<y>(?:19|20)\d\d)(?P<m>[01]\d)(?P<d>[0-3]\d)(?!\d)")),
    ("AAAA-MM", re.compile(r"(?<!\d)(?P<y>(?:19|20)\d\d)[-_.](?P<m>[01]\d)(?![-_.]?\d)")),
    ("AAAAMM", re.compile(r"(?<!\d)(?P<y>(?:19|20)\d\d)(?P<m>[01]\d)(?!\d)")),
)
VERSION = re.compile(r"(?<![a-z])v(?:er(?:sion|sión)?)?[\s_.-]?(?P<n>\d+(?:\.\d+)*)(?![\d.])", re.I)
# Palabras: letras (de cualquier alfabeto) o números; el resto separa.
_WORD = re.compile(r"[^\W\d_]+|\d+")
_CAMEL = re.compile(r"(?<=[a-z])(?=[A-Z])")
# ID de vídeo que yt-dlp añade al final: «título-yZHXoG0-f0k» o «título [FP7QnrPoORM]».
_VIDEO_ID = re.compile(r"(?:-|\s\[)(?P<id>[A-Za-z0-9_-]{11})\]?$")


def strip_video_id(stem: str) -> str:
    """Quita el ID de vídeo que yt-dlp añade al final del nombre, si lo parece."""
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


MIN_TOKEN_LENGTH = 2
# Artículos, preposiciones y conjunciones: no distinguen una carpeta de otra.
NAME_STOPWORDS = frozenset(
    {
        "de",
        "del",
        "el",
        "la",
        "los",
        "las",
        "en",
        "al",
        "un",
        "una",
        "con",
        "por",
        "para",
        "the",
        "of",
        "and",
        "to",
        "in",
        "on",
        "for",
        "an",
    }
)
# Los números largos suelen ser identificadores o timestamps: ruido para el modelo.
MAX_NUMBER_LENGTH = 4


_LATIN_LIMIT = 0x0250  # hasta el final de Latín extendido B


def fold(text: str) -> str:
    """Minúsculas y sin tildes: «Ingeníeria» y «ingenieria» deben ser la misma palabra.

    Solo se quitan las marcas sobre letras latinas: en otros alfabetos forman parte de la
    letra (el dakuten convierte セ «se» en ゼ «ze»).
    """
    out: list[str] = []
    base_is_latin = False
    for c in unicodedata.normalize("NFKD", text.casefold()):
        if unicodedata.combining(c):
            if not base_is_latin:
                out.append(c)
            continue
        base_is_latin = ord(c) < _LATIN_LIMIT
        out.append(c)
    return unicodedata.normalize("NFC", "".join(out))


def split_name(name: str) -> tuple[str, str]:
    """(raíz, extensión en minúsculas sin punto). Respeta extensiones dobles comunes."""
    lowered = name.lower()
    for double in (".tar.gz", ".tar.xz", ".tar.zst", ".tar.bz2"):
        if lowered.endswith(double) and len(name) > len(double):
            return name[: -len(double)], double[1:]
    path = Path(name)
    if not path.suffix:  # incluye «.bashrc»: Path lo trata como raíz sin extensión
        return name, ""
    return path.stem, path.suffix[1:].lower()


@dataclass(frozen=True, slots=True)
class NameDate:
    year: int
    month: int
    day: int | None
    format: str


@dataclass(frozen=True, slots=True)
class NameInfo:
    stem: str
    extension: str
    words: tuple[str, ...]
    date: NameDate | None
    version: str | None
    copy_marker: bool
    has_digits: bool


def find_date(stem: str) -> tuple[NameDate, tuple[int, int]] | None:
    """Primera fecha válida del nombre y su posición."""
    for fmt, pattern in _DATE_PATTERNS:
        for match in pattern.finditer(stem):
            year, month = int(match["y"]), int(match["m"])
            day = int(match["d"]) if "d" in pattern.groupindex else None
            if 1 <= month <= 12 and (day is None or 1 <= day <= 31):
                return NameDate(year, month, day, fmt), match.span()
    return None


def _blank(text: str, span: tuple[int, int]) -> str:
    """Sustituye un tramo por espacios (para que no genere palabras) sin mover posiciones."""
    start, end = span
    return text[:start] + " " * (end - start) + text[end:]


def analyse_name(name: str) -> NameInfo:
    stem, extension = split_name(name)
    clean = strip_copy_marker(strip_video_id(stem))
    found = find_date(clean)
    version = VERSION.search(clean)
    # La fecha y la versión ya son características propias: no se repiten como palabras.
    remaining = _blank(clean, found[1]) if found else clean
    if version:
        remaining = _blank(remaining, version.span())
    spaced = _CAMEL.sub(" ", remaining)  # «CartolaBanco» → «Cartola Banco»
    words = tuple(
        w
        for w in (fold(m) for m in _WORD.findall(spaced))
        if len(w) >= MIN_TOKEN_LENGTH
        and w not in NAME_STOPWORDS
        and not (w.isdigit() and len(w) > MAX_NUMBER_LENGTH)
    )
    return NameInfo(
        stem=stem,
        extension=extension,
        words=words,
        date=found[0] if found else None,
        version=version["n"] if version else None,
        copy_marker=has_copy_marker(name),
        has_digits=any(c.isdigit() for c in stem),
    )
