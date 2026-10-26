"""Metadatos opcionales según el tipo de archivo: MIME, EXIF, PDF, audio y origen.

Son señales **no fiables**: pueden faltar, estar corruptas o ser falsas. Cada extractor
devuelve un diccionario (vacío si no hay nada útil) y nunca lanza excepciones: un
archivo dañado solo significa menos información, no un error.

Los extractores solo leen y tienen límites de tamaño para no atascarse con archivos
enormes o malformados.
"""

from __future__ import annotations

import logging
import mimetypes
import os
import warnings
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import magic  # requiere libmagic (paquete «file», siempre presente en Arch)
import mutagen
from PIL import Image
from pypdf import PdfReader

log = logging.getLogger(__name__)

MAX_PDF_BYTES = 64 * 1024 * 1024
MAX_AUDIO_BYTES = 512 * 1024 * 1024
MAX_TEXT_CHARS = 2000  # texto de la primera página de un PDF para palabras clave
ORIGIN_XATTRS = ("user.xdg.origin.url", "user.xdg.referrer.url")

Metadata = dict[str, str]

# Pillow, pypdf y mutagen informan de archivos dañados con muchos tipos de excepción
# distintos; ningún archivo del usuario debe tumbar la extracción.
_SAFE_ERRORS = (Exception,)


def _safely(kind: str, func: Callable[[Path], Metadata], path: Path) -> Metadata:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            return {k: v for k, v in func(path).items() if v}
    except _SAFE_ERRORS as exc:
        log.debug("Metadatos %s no disponibles para %s: %s", kind, path, exc)
        return {}


def _clean(value: object, max_length: int = 80) -> str:
    text = " ".join(str(value).replace("\x00", "").split())
    return text[:max_length]


# --- MIME -----------------------------------------------------------------------------


def _sniff_mime(path: Path) -> str | None:
    try:
        result = magic.from_file(str(path), mime=True)
    except (OSError, magic.MagicException) as exc:
        log.debug("libmagic no pudo identificar %s: %s", path, exc)
        return None
    return str(result) if result else None


def mime_type(path: Path) -> str | None:
    """MIME por contenido (libmagic); si no se puede, por extensión."""
    sniffed = _sniff_mime(path)
    if sniffed and sniffed not in {"application/octet-stream", "inode/x-empty"}:
        return sniffed
    guessed, _ = mimetypes.guess_type(path.name, strict=False)
    return guessed or sniffed


# --- EXIF --------------------------------------------------------------------------------

_EXIF_TAGS = {0x010F: "marca", 0x0110: "modelo", 0x0131: "software"}
_EXIF_IFD = 0x8769
_DATE_TIME_ORIGINAL = 0x9003


def _exif(path: Path) -> Metadata:
    with Image.open(path) as img:
        exif = img.getexif()
    meta = {name: _clean(exif.get(tag, "")) for tag, name in _EXIF_TAGS.items()}
    taken = exif.get_ifd(_EXIF_IFD).get(_DATE_TIME_ORIGINAL)
    if isinstance(taken, str) and len(taken) >= 4 and taken[:4].isdigit():
        meta["año_captura"] = taken[:4]
    return meta


# --- PDF ---------------------------------------------------------------------------------


def _pdf(path: Path) -> Metadata:
    if path.stat().st_size > MAX_PDF_BYTES:
        return {}
    logging.getLogger("pypdf").setLevel(logging.ERROR)
    reader = PdfReader(path, strict=False)
    if reader.is_encrypted:
        return {"cifrado": "sí"}
    info: dict[str, Any] = dict(reader.metadata or {})
    meta = {
        "titulo": _clean(info.get("/Title", "")),
        "autor": _clean(info.get("/Author", "")),
        "creador": _clean(info.get("/Creator", "")),
        "productor": _clean(info.get("/Producer", "")),
        "paginas": str(len(reader.pages)),
    }
    if reader.pages:
        meta["texto"] = _clean(reader.pages[0].extract_text() or "", MAX_TEXT_CHARS)
    return meta


# --- audio -------------------------------------------------------------------------------


def _audio(path: Path) -> Metadata:
    if path.stat().st_size > MAX_AUDIO_BYTES:
        return {}
    audio = mutagen.File(path, easy=True)
    if audio is None or audio.tags is None:
        return {}

    def first(key: str) -> str:
        values = audio.tags.get(key) or []
        return _clean(values[0]) if values else ""

    return {"artista": first("artist"), "album": first("album"), "genero": first("genre")}


# --- origen de descarga -----------------------------------------------------------------------


def origin_domain(path: Path) -> str | None:
    """Dominio desde el que se descargó (Chromium lo guarda en xattrs; Firefox no)."""
    for attr in ORIGIN_XATTRS:
        try:
            raw = os.getxattr(path, attr)
        except OSError:
            continue
        host = urlsplit(raw.decode("utf-8", "replace")).hostname
        if host:
            return host.removeprefix("www.")
    return None


# --- despacho por tipo ---------------------------------------------------------------------------


def extract_metadata(path: Path, mime: str | None) -> dict[str, Metadata]:
    """Metadatos agrupados por tipo (``exif``, ``pdf``, ``audio``) según el MIME."""
    result: dict[str, Metadata] = {}
    if mime is None:
        return result
    if mime in {"image/jpeg", "image/tiff", "image/webp", "image/heic", "image/png"}:
        result["exif"] = _safely("EXIF", _exif, path)
    elif mime == "application/pdf":
        result["pdf"] = _safely("PDF", _pdf, path)
    elif mime.startswith("audio/") or mime in {"video/mp4", "application/ogg"}:
        result["audio"] = _safely("audio", _audio, path)
    return {kind: meta for kind, meta in result.items() if meta}
