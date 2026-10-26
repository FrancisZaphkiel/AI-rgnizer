"""Imágenes casi idénticas (redimensionadas, recomprimidas, con otro formato).

Cada imagen se resume en un pHash de 64 bits y se agrupan las que distan como mucho
``image_max_distance`` bits (distancia de Hamming). El resultado son **candidatos**: dos
imágenes parecidas pueden ser fotos distintas (una ráfaga, un recorte), así que se
muestran aparte de los duplicados exactos y solo un grupo confirmado explícitamente
(``SimilarGroup.confirm``) puede enviarse a la papelera.

Los pHash se guardan en el índice con la misma invalidación por firma que los hashes
de contenido. Los grupos cuyos archivos son todos idénticos byte a byte se omiten:
ya los muestra la búsqueda de duplicados exactos.
"""

from __future__ import annotations

import logging
import threading
import warnings
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path

import imagehash
import numpy as np
from PIL import Image, ImageOps
from sqlalchemy import select
from sqlalchemy.orm import Session

from organizador.core.config import DuplicateSettings
from organizador.duplicates import hashing
from organizador.duplicates.finder import COMMIT_EVERY_FILES, birth_or_none, under_roots
from organizador.duplicates.index import store_if_unchanged
from organizador.duplicates.keep import KeepChoice, KeepPolicy
from organizador.storage import Database
from organizador.storage.models import IMAGE_UNREADABLE, Directory, File

log = logging.getLogger(__name__)

IMAGE_SUFFIXES = frozenset(
    {".jpg", ".jpeg", ".jpe", ".png", ".webp", ".bmp", ".gif", ".tif", ".tiff"}
)
LOSSY_SUFFIXES = frozenset({".jpg", ".jpeg", ".jpe", ".webp"})
# Orientaciones EXIF que giran 90°: el ancho y el alto se intercambian al mostrarla.
_EXIF_ORIENTATION = 0x0112
_ROTATED_ORIENTATIONS = frozenset({5, 6, 7, 8})
# Errores con los que Pillow señala una imagen corrupta, truncada o no soportada.
_DECODE_ERRORS = (OSError, ValueError, SyntaxError, Image.DecompressionBombError)


@dataclass(frozen=True, slots=True)
class ImageInfo:
    perceptual_hash: str
    width: int
    height: int


def image_info(path: Path) -> ImageInfo:
    """pHash (tras aplicar la orientación EXIF) y resolución tal como se muestra la imagen."""
    with warnings.catch_warnings():
        # Por encima de MAX_IMAGE_PIXELS, Pillow avisa; por encima del doble, lanza error.
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        with Image.open(path) as img:
            width, height = img.size
            if img.getexif().get(_EXIF_ORIENTATION, 1) in _ROTATED_ORIENTATIONS:
                width, height = height, width
            img.draft("L", (256, 256))  # JPEG: decodifica a escala reducida, mucho más rápido
            upright = ImageOps.exif_transpose(img)
            return ImageInfo(str(imagehash.phash(upright)), width, height)


def is_image_name(name: str) -> bool:
    return Path(name).suffix.lower() in IMAGE_SUFFIXES


@dataclass(frozen=True, slots=True)
class SimilarFile:
    path: Path
    size: int
    mtime_ns: int
    width: int
    height: int
    perceptual_hash: str
    # BLAKE3: se usa para verificar que el archivo no cambió antes de enviarlo a la papelera.
    content_hash: str
    birth_ns: int | None = None

    @property
    def pixels(self) -> int:
        return self.width * self.height


def hamming(a: str, b: str) -> int:
    return (int(a, 16) ^ int(b, 16)).bit_count()


@dataclass(frozen=True, slots=True)
class SimilarGroup:
    files: tuple[SimilarFile, ...]
    keep: SimilarFile
    keep_reason: str

    def with_keep(self, keep: SimilarFile) -> SimilarGroup:
        if keep not in self.files:
            raise ValueError(f"{keep.path} no pertenece al grupo")
        return replace(self, keep=keep, keep_reason="elegida por el usuario")

    @property
    def redundant(self) -> tuple[SimilarFile, ...]:
        return tuple(f for f in self.files if f is not self.keep)

    def distance_to_keep(self, f: SimilarFile) -> int:
        return hamming(f.perceptual_hash, self.keep.perceptual_hash)

    @property
    def redundant_bytes(self) -> int:
        return sum(f.size for f in self.redundant)

    def confirm(self) -> ConfirmedSimilarGroup:
        """Única forma de obtener un grupo que se pueda enviar a la papelera.

        Llamar solo tras mostrar el grupo al usuario y recibir su confirmación explícita.
        """
        return ConfirmedSimilarGroup(self)


@dataclass(frozen=True, slots=True)
class ConfirmedSimilarGroup:
    group: SimilarGroup


def is_lossy(path: Path) -> bool:
    """JPEG y WebP pierden calidad al guardar; PNG, BMP, GIF y TIFF no (WebP puede ser
    sin pérdida, pero se trata como con pérdida: no se distingue sin decodificar)."""
    return path.suffix.lower() in LOSSY_SUFFIXES


def choose_image_keep(policy: KeepPolicy, files: Sequence[SimilarFile]) -> KeepChoice[SimilarFile]:
    """Criterios, en orden:

    1. mayor resolución;
    2. formato sin pérdida frente a uno con pérdida;
    3. entre formatos con pérdida, el archivo más grande (menos compresión). Entre PNG no
       se aplica: un PNG más grande solo tiene más contenido, no más calidad;
    4. los criterios generales de ``KeepPolicy``.
    """
    best_pixels = max(f.pixels for f in files)
    pool = [f for f in files if f.pixels == best_pixels]
    if len(pool) == 1:
        keep = pool[0]
        return KeepChoice(keep, f"tiene la mayor resolución ({keep.width}×{keep.height})")

    lossless = [f for f in pool if not is_lossy(f.path)]
    if len(lossless) == 1:
        return KeepChoice(lossless[0], "misma resolución y formato sin pérdida")
    if not lossless:
        best_size = max(f.size for f in pool)
        largest = [f for f in pool if f.size == best_size]
        if len(largest) == 1:
            return KeepChoice(largest[0], "misma resolución y menos compresión")
        pool = largest
    else:
        pool = lossless
    return policy.choose(pool)


@dataclass(slots=True)
class SimilarStats:
    images: int = 0
    hashed: int = 0
    unreadable: int = 0
    stale: int = 0
    errors: int = 0
    cancelled: bool = False


@dataclass(slots=True)
class SimilarReport:
    groups: list[SimilarGroup] = field(default_factory=list)
    stats: SimilarStats = field(default_factory=SimilarStats)


ProgressCallback = Callable[[SimilarStats], None]


@dataclass(slots=True)
class _Image:
    file_id: int
    path: Path
    signature: hashing.FileSignature
    perceptual_hash: str | None
    width: int | None
    height: int | None
    content_hash: str | None

    @property
    def size(self) -> int:
        return self.signature[0]

    @property
    def inode_key(self) -> tuple[int, int]:
        return (self.signature[4], self.signature[3])


def cluster(hashes: Sequence[str], max_distance: int) -> list[list[int]]:
    """Agrupa índices en torno a un centro: cada miembro dista ≤ ``max_distance`` del centro.

    No es transitivo a propósito: con «A parecida a B, B parecida a C» encadenado, una
    serie de capturas de pantalla parecidas acabaría en un único grupo gigante.
    Así, dos miembros de un grupo distan como mucho ``2 × max_distance`` entre sí.
    """
    if len(hashes) < 2:
        return []
    values = np.array([int(h, 16) for h in hashes], dtype=np.uint64)
    unassigned = np.ones(len(values), dtype=bool)
    groups: list[list[int]] = []
    for center in range(len(values)):
        if not unassigned[center]:
            continue
        near = unassigned & (np.bitwise_count(values ^ values[center]) <= max_distance)
        members = [int(i) for i in np.flatnonzero(near)]
        unassigned[members] = False
        if len(members) >= 2:
            groups.append(members)
    return groups


class SimilarImageFinder:
    def __init__(self, db: Database, settings: DuplicateSettings, policy: KeepPolicy) -> None:
        self._db = db
        self._settings = settings
        self._policy = policy

    def find(
        self,
        roots: Sequence[Path],
        *,
        cancel: threading.Event | None = None,
        progress: ProgressCallback | None = None,
    ) -> SimilarReport:
        """Agrupa imágenes indexadas bajo ``roots`` (solo lectura sobre los archivos)."""
        resolved = [r.expanduser().resolve() for r in roots]
        report = SimilarReport()
        with self._db.new_session() as session:
            job = _SimilarJob(self._settings, session, report.stats, cancel, progress)
            images = job.analyse(resolved)
            if not report.stats.cancelled:
                clusters = cluster([i.perceptual_hash or "" for i in images], self._max_distance)
                report.groups = [g for c in clusters if (g := job.build(images, c, self._policy))]
            session.commit()
        report.groups.sort(key=lambda g: (-g.redundant_bytes, str(g.keep.path)))
        log.info("Búsqueda de imágenes similares en %s: %s", resolved, report.stats)
        return report

    @property
    def _max_distance(self) -> int:
        return max(0, min(64, self._settings.image_max_distance))


class _SimilarJob:
    def __init__(
        self,
        settings: DuplicateSettings,
        session: Session,
        stats: SimilarStats,
        cancel: threading.Event | None,
        progress: ProgressCallback | None,
    ) -> None:
        self.settings = settings
        self.session = session
        self.stats = stats
        self.cancel = cancel
        self.progress = progress
        self._pending_writes = 0

    def cancelled(self) -> bool:
        if self.cancel is not None and self.cancel.is_set():
            self.stats.cancelled = True
        return self.stats.cancelled

    def analyse(self, roots: Sequence[Path]) -> list[_Image]:
        """Imágenes legibles y vigentes, una por inode, con su pHash."""
        result: list[_Image] = []
        seen_inodes: set[tuple[int, int]] = set()
        for image in self.load(roots):
            if self.cancelled():
                break
            # Los hardlinks son el mismo archivo: compararlos no aporta nada.
            if image.inode_key in seen_inodes:
                continue
            if self.ensure_info(image):
                seen_inodes.add(image.inode_key)
                result.append(image)
        return result

    def load(self, roots: Sequence[Path]) -> list[_Image]:
        rows = self.session.execute(
            select(
                File.id,
                Directory.path,
                File.name,
                File.size,
                File.mtime_ns,
                File.ctime_ns,
                File.inode,
                File.device,
                File.perceptual_hash,
                File.image_width,
                File.image_height,
                File.content_hash,
            )
            .join(Directory)
            .where(under_roots(roots), File.size >= max(1, self.settings.min_size))
            .order_by(Directory.path, File.name)
        )
        images = [
            _Image(fid, Path(d) / n, (size, mt, ct, ino, dev), ph, w, h, ch)
            for fid, d, n, size, mt, ct, ino, dev, ph, w, h, ch in rows
            if is_image_name(n)
        ]
        self.stats.images = len(images)
        return images

    def unchanged(self, image: _Image) -> bool:
        try:
            current = hashing.signature(image.path.lstat())
        except OSError:
            self.stats.stale += 1
            return False
        if current != image.signature:
            self.stats.stale += 1
            return False
        return True

    def ensure_info(self, image: _Image) -> bool:
        if not self.unchanged(image):
            return False
        if image.perceptual_hash == IMAGE_UNREADABLE:
            self.stats.unreadable += 1
            return False
        if image.perceptual_hash is not None:
            return True

        values: dict[str, object]
        try:
            info = image_info(image.path)
        except PermissionError as exc:
            log.debug("Sin permiso para leer %s: %s", image.path, exc)
            self.stats.errors += 1
            return False
        except _DECODE_ERRORS as exc:
            # Se recuerda para no reintentar hasta que el archivo cambie.
            log.debug("Imagen no decodificable %s: %s", image.path, exc)
            values = {"perceptual_hash": IMAGE_UNREADABLE}
            ok = False
            self.stats.unreadable += 1
        else:
            image.perceptual_hash = info.perceptual_hash
            image.width, image.height = info.width, info.height
            values = {
                "perceptual_hash": info.perceptual_hash,
                "image_width": info.width,
                "image_height": info.height,
            }
            ok = True
            self.stats.hashed += 1

        if not self.unchanged(image):  # cambió mientras se leía
            return False
        self.store(image, values)
        if self.progress is not None:
            self.progress(self.stats)
        return ok

    def store(self, image: _Image, values: dict[str, object]) -> None:
        store_if_unchanged(self.session, [image.file_id], image.signature, values)
        self._pending_writes += 1
        if self._pending_writes >= COMMIT_EVERY_FILES:
            self.session.commit()
            self._pending_writes = 0

    def build(
        self, images: Sequence[_Image], members: Sequence[int], policy: KeepPolicy
    ) -> SimilarGroup | None:
        files: list[SimilarFile] = []
        for i in members:
            image = images[i]
            content = self.content_hash(image)
            if content is None:
                continue
            assert image.perceptual_hash is not None
            files.append(
                SimilarFile(
                    path=image.path,
                    size=image.size,
                    mtime_ns=image.signature[1],
                    width=image.width or 0,
                    height=image.height or 0,
                    perceptual_hash=image.perceptual_hash,
                    content_hash=content,
                    birth_ns=birth_or_none(image.path),
                )
            )
        # Todos idénticos byte a byte: es un grupo de duplicados exactos, no de similares.
        if len(files) < 2 or len({f.content_hash for f in files}) == 1:
            return None
        files.sort(key=lambda f: str(f.path))
        choice = choose_image_keep(policy, files)
        return SimilarGroup(tuple(files), choice.keep, choice.reason)

    def content_hash(self, image: _Image) -> str | None:
        if image.content_hash is not None:
            return image.content_hash
        try:
            digest = hashing.full_hash(image.path)
        except OSError as exc:
            log.debug("No se pudo leer %s: %s", image.path, exc)
            self.stats.errors += 1
            return None
        if not self.unchanged(image):
            return None
        image.content_hash = digest
        self.store(image, {"content_hash": digest})
        return digest
