"""Persistencia del journal de acciones.

Cada transición se confirma en su propia transacción para que el estado en disco
refleje siempre el último paso realizado, incluso si el proceso muere después.

Estados: ``pending → in_progress → done → reverted``, o ``failed``.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from sqlalchemy import or_, select

from airgnizer.actions.fingerprint import Fingerprint
from airgnizer.storage import Database
from airgnizer.storage.models import ACTIVE_STATES, ActionJournal, ActionKind, ActionState

_ALLOWED: dict[ActionState, frozenset[ActionState]] = {
    ActionState.PENDING: frozenset({ActionState.IN_PROGRESS, ActionState.FAILED, ActionState.DONE}),
    ActionState.IN_PROGRESS: frozenset({ActionState.DONE, ActionState.FAILED}),
    ActionState.DONE: frozenset({ActionState.REVERTED}),
    ActionState.FAILED: frozenset(),
    ActionState.REVERTED: frozenset(),
}


class InvalidTransitionError(RuntimeError):
    pass


class Journal:
    def __init__(self, db: Database) -> None:
        self._db = db

    def begin(
        self,
        kind: ActionKind,
        source: Path,
        destination: Path | None,
        fingerprint: Fingerprint,
        *,
        origin: str,
        temp_path: Path | None = None,
        reverts_id: str | None = None,
        batch_id: str | None = None,
    ) -> ActionJournal:
        entry = ActionJournal(
            kind=kind,
            state=ActionState.PENDING,
            source=str(source),
            destination=str(destination) if destination else None,
            temp_path=str(temp_path) if temp_path else None,
            size=fingerprint.size,
            mtime_ns=fingerprint.mtime_ns,
            inode=fingerprint.inode,
            device=fingerprint.device,
            content_hash=fingerprint.content_hash,
            origin=origin,
            reverts_id=reverts_id,
            batch_id=batch_id,
        )
        with self._db.session() as s:
            s.add(entry)
        return entry

    def transition(
        self,
        entry_id: str,
        state: ActionState,
        *,
        error: str | None = None,
        **fields: str | None,
    ) -> ActionJournal:
        """Cambia de estado validando la transición; ``fields`` actualiza columnas de texto."""
        with self._db.session() as s:
            entry = s.get_one(ActionJournal, entry_id)
            if state not in _ALLOWED[entry.state]:
                raise InvalidTransitionError(f"{entry.id}: {entry.state} → {state}")
            entry.state = state
            if error is not None:
                entry.error = error
            for name, value in fields.items():
                if name not in {"destination", "temp_path"}:
                    raise ValueError(f"Campo no actualizable: {name}")
                setattr(entry, name, value)
        return entry

    def complete_undo(self, undo_id: str, original_id: str) -> None:
        """Marca la reversión como hecha y la original como revertida, atómicamente."""
        with self._db.session() as s:
            undo = s.get_one(ActionJournal, undo_id)
            original = s.get_one(ActionJournal, original_id)
            if ActionState.DONE not in _ALLOWED[undo.state] or original.state != ActionState.DONE:
                raise InvalidTransitionError(f"{undo_id}/{original_id}")
            undo.state = ActionState.DONE
            original.state = ActionState.REVERTED

    def get(self, entry_id: str) -> ActionJournal | None:
        with self._db.session() as s:
            return s.get(ActionJournal, entry_id)

    def active(self) -> Sequence[ActionJournal]:
        with self._db.session() as s:
            return s.scalars(
                select(ActionJournal)
                .where(ActionJournal.state.in_(ACTIVE_STATES))
                .order_by(ActionJournal.created_at)
            ).all()

    def has_active_for(self, *paths: Path) -> bool:
        names = [str(p) for p in paths]
        with self._db.session() as s:
            return (
                s.scalar(
                    select(ActionJournal.id)
                    .where(
                        ActionJournal.state.in_(ACTIVE_STATES),
                        or_(ActionJournal.source.in_(names), ActionJournal.destination.in_(names)),
                    )
                    .limit(1)
                )
                is not None
            )

    def history(self, limit: int = 50) -> Sequence[ActionJournal]:
        with self._db.session() as s:
            return s.scalars(
                select(ActionJournal).order_by(ActionJournal.created_at.desc()).limit(limit)
            ).all()

    def find_by_prefix(self, prefix: str) -> Sequence[ActionJournal]:
        with self._db.session() as s:
            return s.scalars(
                select(ActionJournal).where(ActionJournal.id.startswith(prefix, autoescape=True))
            ).all()
