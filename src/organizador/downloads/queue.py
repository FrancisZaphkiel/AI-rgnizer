"""Cola de sugerencias persistida en la tabla ``suggestions``."""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

from sqlalchemy import select

from organizador.downloads.completion import Arrival
from organizador.learning.model import Prediction
from organizador.storage import Database
from organizador.storage.models import DownloadSuggestion, SuggestionState


class SuggestionNotFoundError(LookupError):
    pass


class SuggestionQueue:
    def __init__(self, db: Database) -> None:
        self._db = db

    def knows(self, arrival: Arrival) -> bool:
        """¿Esta llegada (ruta, tamaño, mtime) ya tiene sugerencia, en cualquier estado?"""
        with self._db.session() as s:
            return (
                s.scalar(
                    select(DownloadSuggestion.id).where(
                        DownloadSuggestion.path == str(arrival.path),
                        DownloadSuggestion.size == arrival.size,
                        DownloadSuggestion.mtime_ns == arrival.mtime_ns,
                    )
                )
                is not None
            )

    def add(self, arrival: Arrival, prediction: Prediction) -> DownloadSuggestion | None:
        """Registra la llegada; ``None`` si esa misma llegada ya tenía sugerencia."""
        best = prediction.best
        with self._db.session() as s:
            exists = s.scalar(
                select(DownloadSuggestion.id).where(
                    DownloadSuggestion.path == str(arrival.path),
                    DownloadSuggestion.size == arrival.size,
                    DownloadSuggestion.mtime_ns == arrival.mtime_ns,
                )
            )
            if exists is not None:
                return None
            # Una versión anterior del mismo nombre que seguía pendiente ya no existe.
            for old in s.scalars(
                select(DownloadSuggestion).where(
                    DownloadSuggestion.path == str(arrival.path),
                    DownloadSuggestion.state == SuggestionState.PENDING,
                )
            ):
                old.state = SuggestionState.GONE
            row = DownloadSuggestion(
                path=str(arrival.path),
                size=arrival.size,
                mtime_ns=arrival.mtime_ns,
                inode=arrival.inode,
                device=arrival.device,
                destination=best.label if best else None,
                confidence=best.probability if best else None,
                source=prediction.source if best else None,
                alternatives=json.dumps(
                    [[x.label, x.probability] for x in prediction.suggestions], ensure_ascii=False
                ),
                state=SuggestionState.PENDING,
            )
            s.add(row)
        return row

    def pending(self) -> Sequence[DownloadSuggestion]:
        with self._db.session() as s:
            return s.scalars(
                select(DownloadSuggestion)
                .where(DownloadSuggestion.state == SuggestionState.PENDING)
                .order_by(DownloadSuggestion.id)
            ).all()

    def get(self, suggestion_id: int) -> DownloadSuggestion:
        with self._db.session() as s:
            row = s.get(DownloadSuggestion, suggestion_id)
            if row is None:
                raise SuggestionNotFoundError(f"No hay ninguna sugerencia {suggestion_id}")
            return row

    def resolve(
        self,
        suggestion_id: int,
        state: SuggestionState,
        *,
        chosen: str | None = None,
        action_id: str | None = None,
    ) -> DownloadSuggestion:
        with self._db.session() as s:
            row = s.get(DownloadSuggestion, suggestion_id)
            if row is None:
                raise SuggestionNotFoundError(f"No hay ninguna sugerencia {suggestion_id}")
            row.state = state
            row.chosen = chosen
            row.action_id = action_id
        return row

    def expire_missing(self) -> int:
        """Marca ``gone`` las pendientes cuyo archivo ya no está o cambió.

        Si falta la carpeta entera (disco externo desconectado) la sugerencia se conserva:
        el archivo volverá a estar al conectar el disco.
        """
        expired = 0
        with self._db.session() as s:
            for row in s.scalars(
                select(DownloadSuggestion).where(
                    DownloadSuggestion.state == SuggestionState.PENDING
                )
            ):
                path = Path(row.path)
                if not path.parent.is_dir():
                    continue
                current = Arrival.of(path)
                if current is None or (current.size, current.mtime_ns) != (row.size, row.mtime_ns):
                    row.state = SuggestionState.GONE
                    expired += 1
        return expired


def available(row: DownloadSuggestion) -> bool:
    """La carpeta del archivo está accesible (su disco está conectado)."""
    return Path(row.path).parent.is_dir()


def alternatives_of(row: DownloadSuggestion) -> list[tuple[str, float]]:
    return [(label, float(p)) for label, p in json.loads(row.alternatives)]
