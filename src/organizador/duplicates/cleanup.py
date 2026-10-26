"""Envío a la papelera de las copias sobrantes de un grupo de duplicados.

Antes de tocar nada se vuelve a comprobar, leyendo el disco, que la copia conservada
existe y conserva el contenido del grupo: si no, se aborta el grupo entero. Cada copia
sobrante se envía con ``ActionService.trash(expected_hash=...)``, que recalcula el hash
bajo el bloqueo; todas quedan en el journal y se pueden deshacer una a una.

Para imágenes similares (que no son idénticas) solo se acepta un
``ConfirmedSimilarGroup``: el tipo obliga a pasar por una confirmación explícita.
"""

from __future__ import annotations

import logging
import stat
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from organizador.actions import ActionError, ActionResult, ActionService
from organizador.actions.errors import SourceChangedError, SourceMissingError
from organizador.duplicates.finder import DuplicateFile, DuplicateGroup
from organizador.duplicates.hashing import full_hash
from organizador.duplicates.similar import ConfirmedSimilarGroup, SimilarFile

log = logging.getLogger(__name__)


@dataclass(slots=True)
class CleanupResult:
    trashed: list[ActionResult] = field(default_factory=list)
    failed: list[tuple[DuplicateFile | SimilarFile, ActionError]] = field(default_factory=list)


def verify_unchanged(path: Path, content_hash: str) -> None:
    """Lanza ``ActionError`` si ``path`` ya no tiene el contenido ``content_hash``."""
    try:
        st = path.lstat()
        intact = stat.S_ISREG(st.st_mode) and full_hash(path) == content_hash
    except FileNotFoundError as exc:
        raise SourceMissingError(path) from exc
    except OSError as exc:
        raise ActionError(f"No se pudo verificar la copia a conservar {path}: {exc}") from exc
    if not intact:
        raise SourceChangedError(path)


def _trash_all(
    items: Iterable[tuple[DuplicateFile | SimilarFile, str]], service: ActionService
) -> CleanupResult:
    result = CleanupResult()
    for item, expected in items:
        try:
            result.trashed.append(service.trash(item.path, expected_hash=expected))
        except ActionError as exc:
            log.warning("No se envió %s a la papelera: %s", item.path, exc)
            result.failed.append((item, exc))
    return result


def trash_redundant(group: DuplicateGroup, service: ActionService) -> CleanupResult:
    """Envía ``group.redundant`` a la papelera; la copia conservada y sus hardlinks no se tocan.

    Un fallo en una copia no detiene las demás; se informa en ``CleanupResult.failed``.
    """
    verify_unchanged(group.keep.path, group.content_hash)
    return _trash_all(((f, group.content_hash) for f in group.redundant), service)


def trash_similar(confirmed: ConfirmedSimilarGroup, service: ActionService) -> CleanupResult:
    """Envía a la papelera las imágenes de un grupo similar **confirmado**, salvo la elegida.

    Cada archivo se verifica contra su propio hash: si cambió desde que se mostró, se omite.
    """
    if not isinstance(confirmed, ConfirmedSimilarGroup):  # protección también sin mypy
        raise TypeError("Solo se aceptan grupos similares confirmados por el usuario")
    group = confirmed.group
    verify_unchanged(group.keep.path, group.keep.content_hash)
    return _trash_all(((f, f.content_hash) for f in group.redundant), service)
