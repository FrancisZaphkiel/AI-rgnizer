"""Servicio de acciones seguras: única puerta de entrada para modificar archivos.

Toda operación sigue el mismo protocolo:

1. validar el origen y el destino (sin tocar nada);
2. tomar los bloqueos y registrar la acción en el journal como ``pending``;
3. pasar a ``in_progress`` y ejecutar;
4. marcar ``done`` (o ``failed``).

Si el proceso muere entre medias, ``recover()`` inspecciona el disco y deja cada
acción en un estado coherente sin perder ni duplicar archivos.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from organizador.actions import fsops, trash
from organizador.actions.errors import (
    ActionError,
    ConcurrentOperationError,
    DestinationOccupiedError,
    InvalidTargetError,
    NotUndoableError,
    SourceChangedError,
    SourceMissingError,
)
from organizador.actions.fingerprint import Fingerprint
from organizador.actions.journal import Journal
from organizador.actions.locks import PathLocks, process_lock
from organizador.core.config import ActionSettings
from organizador.storage import Database
from organizador.storage.models import ActionJournal, ActionKind, ActionState

log = logging.getLogger(__name__)

UNDOABLE = frozenset({ActionKind.MOVE, ActionKind.RENAME, ActionKind.TRASH})
_MAX_COLLISION_ATTEMPTS = 10_000


@dataclass(frozen=True, slots=True)
class ActionResult:
    action_id: str
    source: Path
    destination: Path


@dataclass(frozen=True, slots=True)
class RecoveryOutcome:
    action_id: str
    state: ActionState
    detail: str


def free_name(target: Path) -> Path:
    """Primer nombre libre con el estilo de los gestores de archivos: ``nombre (2).ext``."""
    if not target.exists():
        return target
    stem, suffix = target.stem, target.suffix
    for n in range(2, _MAX_COLLISION_ATTEMPTS):
        candidate = target.with_name(f"{stem} ({n}){suffix}")
        if not candidate.exists():
            return candidate
    raise DestinationOccupiedError(target)


def _fingerprint_of(entry: ActionJournal) -> Fingerprint:
    return Fingerprint(entry.size, entry.mtime_ns, entry.inode, entry.device, entry.content_hash)


class ActionService:
    def __init__(
        self,
        db: Database,
        settings: ActionSettings,
        lock_file: Path,
        *,
        origin: str = "cli",
    ) -> None:
        self._journal = Journal(db)
        self._settings = settings
        self._lock_file = lock_file
        self._origin = origin
        self._locks = PathLocks()

    # --- API pública ---------------------------------------------------------

    def move(self, source: Path, dest_dir: Path) -> ActionResult:
        """Mueve ``source`` a ``dest_dir``; si el nombre está ocupado usa ``nombre (n).ext``."""
        source = self._validate_source(source)
        dest_dir = dest_dir.expanduser().absolute()
        if not dest_dir.is_dir():
            raise InvalidTargetError(f"El destino no es un directorio: {dest_dir}")
        if dest_dir == source.parent:
            raise InvalidTargetError(f"El archivo ya está en {dest_dir}")
        with self._guard(source):
            target = free_name(dest_dir / source.name)
            return self._relocate(ActionKind.MOVE, source, target)

    def rename(self, source: Path, new_name: str) -> ActionResult:
        """Renombra en el mismo directorio; si el nombre está ocupado, se rechaza."""
        source = self._validate_source(source)
        if not new_name or new_name in {".", ".."} or "/" in new_name or "\0" in new_name:
            raise InvalidTargetError(f"Nombre no válido: {new_name!r}")
        target = source.with_name(new_name)
        if target == source:
            raise InvalidTargetError("El nombre nuevo es igual al actual")
        with self._guard(source, target):
            if target.exists():
                raise DestinationOccupiedError(target, free_name(target))
            return self._relocate(ActionKind.RENAME, source, target)

    def trash(self, source: Path) -> ActionResult:
        """Envía a la papelera del sistema. Nunca borra."""
        source = self._validate_source(source)
        with self._guard(source):
            fp = self._fingerprint(source)
            entry = self._journal.begin(ActionKind.TRASH, source, None, fp, origin=self._origin)
            self._journal.transition(entry.id, ActionState.IN_PROGRESS)
            try:
                item = trash.move_to_trash(source)
            except OSError as exc:
                self._journal.transition(entry.id, ActionState.FAILED, error=str(exc))
                raise ActionError(f"No se pudo enviar a la papelera: {exc}") from exc
            self._journal.transition(entry.id, ActionState.DONE, destination=str(item.files_path))
            return ActionResult(entry.id, source, item.files_path)

    def undo(self, action_id: str) -> ActionResult:
        """Revierte una acción ``done``. Se niega si el archivo cambió o el origen está ocupado."""
        original = self._resolve_id(action_id)
        if original.kind not in UNDOABLE:
            raise NotUndoableError(f"Las acciones de tipo {original.kind} no se pueden deshacer")
        if original.state != ActionState.DONE or original.destination is None:
            raise NotUndoableError(f"La acción está en estado {original.state}")

        current = Path(original.destination)
        restore_to = Path(original.source)
        with self._guard(current, restore_to):
            if not current.exists():
                raise SourceMissingError(current)
            if not _fingerprint_of(original).matches(current):
                raise SourceChangedError(current)
            if restore_to.exists():
                raise DestinationOccupiedError(restore_to, free_name(restore_to))
            restore_to.parent.mkdir(parents=True, exist_ok=True)
            result = self._relocate(ActionKind.UNDO, current, restore_to, reverts=original)
            return result

    def recover(self) -> list[RecoveryOutcome]:
        """Resuelve las acciones que quedaron a medias (llamar al arrancar)."""
        outcomes: list[RecoveryOutcome] = []
        with process_lock(self._lock_file):
            for entry in self._journal.active():
                outcome = self._recover_entry(entry)
                log.warning(
                    "Recuperada acción %s: %s (%s)", entry.id, outcome.state, outcome.detail
                )
                outcomes.append(outcome)
        return outcomes

    def history(self, limit: int = 50) -> Sequence[ActionJournal]:
        return self._journal.history(limit)

    # --- internos ------------------------------------------------------------

    @contextmanager
    def _guard(self, *paths: Path) -> Iterator[None]:
        with self._locks.hold(*paths), process_lock(self._lock_file):
            if self._journal.has_active_for(*paths):
                raise ConcurrentOperationError(paths[0])
            yield

    def _validate_source(self, source: Path) -> Path:
        source = source.expanduser().absolute()
        if not source.exists() and not source.is_symlink():
            raise SourceMissingError(source)
        if source.is_symlink() or not source.is_file():
            raise InvalidTargetError(f"Solo se admiten archivos regulares: {source}")
        return source

    def _fingerprint(self, path: Path) -> Fingerprint:
        return Fingerprint.of(path, hash_max_bytes=self._settings.fingerprint_hash_max_bytes)

    def _resolve_id(self, action_id: str) -> ActionJournal:
        matches = self._journal.find_by_prefix(action_id)
        if not matches:
            raise NotUndoableError(f"No existe la acción {action_id}")
        if len(matches) > 1:
            raise NotUndoableError(f"El prefijo {action_id} es ambiguo")
        return matches[0]

    def _relocate(
        self,
        kind: ActionKind,
        source: Path,
        target: Path,
        *,
        reverts: ActionJournal | None = None,
    ) -> ActionResult:
        fp = self._fingerprint(source)
        temp = fsops.temp_path_for(target, uuid.uuid4().hex[:12])
        entry = self._journal.begin(
            kind,
            source,
            target,
            fp,
            origin=self._origin,
            temp_path=temp,
            reverts_id=reverts.id if reverts else None,
        )
        self._journal.transition(entry.id, ActionState.IN_PROGRESS)
        try:
            self._do_relocate(source, target, temp, fp)
        except FileExistsError as exc:
            self._journal.transition(entry.id, ActionState.FAILED, error=str(exc))
            raise DestinationOccupiedError(target, free_name(target)) from exc
        except OSError as exc:
            fsops.discard_temp(temp)
            self._journal.transition(entry.id, ActionState.FAILED, error=str(exc))
            raise ActionError(f"No se pudo mover {source}: {exc}") from exc

        if reverts is None:
            self._journal.transition(entry.id, ActionState.DONE)
        else:
            if reverts.kind is ActionKind.TRASH:
                fsops.remove_trashinfo(trash.info_path_for(source))
            self._journal.complete_undo(entry.id, reverts.id)
        return ActionResult(entry.id, source, target)

    def _do_relocate(self, source: Path, target: Path, temp: Path, fp: Fingerprint) -> None:
        try:
            fsops.move_no_replace(source, target)
        except fsops.CrossDeviceError:
            expected = fp.content_hash or fsops.hash_file(source)
            fsops.copy_verify_unlink(source, target, temp, expected)
            return
        fsops.fsync_dir(target.parent)
        if source.parent != target.parent:
            fsops.fsync_dir(source.parent)

    # --- recuperación --------------------------------------------------------

    def _recover_entry(self, entry: ActionJournal) -> RecoveryOutcome:
        if entry.kind is ActionKind.TRASH:
            state, detail = self._recover_trash(entry)
        else:
            state, detail = self._recover_relocation(entry)

        if state is ActionState.DONE and entry.kind is ActionKind.UNDO and entry.reverts_id:
            reverted = self._journal.get(entry.reverts_id)
            if reverted is not None and reverted.kind is ActionKind.TRASH:
                fsops.remove_trashinfo(trash.info_path_for(Path(entry.source)))
            self._journal.complete_undo(entry.id, entry.reverts_id)
        else:
            self._journal.transition(
                entry.id,
                state,
                error=detail if state is ActionState.FAILED else None,
                destination=entry.destination,
            )
        return RecoveryOutcome(entry.id, state, detail)

    def _recover_relocation(self, entry: ActionJournal) -> tuple[ActionState, str]:
        source = Path(entry.source)
        target = Path(entry.destination or "")
        fp = _fingerprint_of(entry)
        if entry.temp_path:
            # Un temporal nunca se publica sin antes renombrarse: si existe, sobra.
            fsops.discard_temp(Path(entry.temp_path))

        src_exists, dst_exists = source.exists(), target.exists()
        if src_exists and not dst_exists:
            return ActionState.FAILED, "Interrumpida antes de mover; el archivo sigue en su lugar"
        if dst_exists and not src_exists:
            if fp.matches(target):
                return ActionState.DONE, "El movimiento se había completado"
            return ActionState.FAILED, "El destino no coincide con el original; revisar a mano"
        if src_exists and dst_exists:
            if fsops.complete_interrupted_copy(source, target):
                return ActionState.DONE, "Copia verificada; se completó el movimiento"
            return ActionState.FAILED, "Origen y destino existen y difieren; no se tocó nada"
        return ActionState.FAILED, "El archivo no está ni en el origen ni en el destino"

    def _recover_trash(self, entry: ActionJournal) -> tuple[ActionState, str]:
        source = Path(entry.source)
        if source.exists():
            return ActionState.FAILED, "Interrumpida antes de enviar a la papelera"
        item = trash.locate(source, entry.inode, entry.device)
        if item is None:
            return ActionState.FAILED, "El archivo no está en su lugar ni en la papelera"
        entry.destination = str(item.files_path)
        return ActionState.DONE, "El envío a la papelera se había completado"
