"""Envío a la papelera de las copias sobrantes de un grupo de duplicados exactos.

Antes de tocar nada se vuelve a comprobar, leyendo el disco, que la copia conservada
existe y conserva el contenido del grupo: si no, se aborta el grupo entero. Cada copia
sobrante se envía con ``ActionService.trash(expected_hash=...)``, que recalcula el hash
bajo el bloqueo; todas quedan en el journal y se pueden deshacer una a una.
"""

from __future__ import annotations

import logging
import stat
from dataclasses import dataclass, field

from organizador.actions import ActionError, ActionResult, ActionService
from organizador.actions.errors import SourceChangedError, SourceMissingError
from organizador.duplicates.finder import DuplicateFile, DuplicateGroup
from organizador.duplicates.hashing import full_hash

log = logging.getLogger(__name__)


@dataclass(slots=True)
class CleanupResult:
    trashed: list[ActionResult] = field(default_factory=list)
    failed: list[tuple[DuplicateFile, ActionError]] = field(default_factory=list)


def verify_keep(group: DuplicateGroup) -> None:
    """Lanza ``ActionError`` si la copia a conservar ya no tiene el contenido del grupo."""
    path = group.keep.path
    try:
        st = path.lstat()
        intact = stat.S_ISREG(st.st_mode) and full_hash(path) == group.content_hash
    except FileNotFoundError as exc:
        raise SourceMissingError(path) from exc
    except OSError as exc:
        raise ActionError(f"No se pudo verificar la copia a conservar {path}: {exc}") from exc
    if not intact:
        raise SourceChangedError(path)


def trash_redundant(group: DuplicateGroup, service: ActionService) -> CleanupResult:
    """Envía ``group.redundant`` a la papelera; la copia conservada y sus hardlinks no se tocan.

    Un fallo en una copia no detiene las demás; se informa en ``CleanupResult.failed``.
    """
    verify_keep(group)
    result = CleanupResult()
    for dup in group.redundant:
        try:
            result.trashed.append(service.trash(dup.path, expected_hash=group.content_hash))
        except ActionError as exc:
            log.warning("No se envió %s a la papelera: %s", dup.path, exc)
            result.failed.append((dup, exc))
    return result
