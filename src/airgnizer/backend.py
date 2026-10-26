"""Operaciones que modifican archivos, en local o a través del servicio.

Mientras el servicio está en marcha es el **único escritor**: la CLI y la GUI le piden las
operaciones por D-Bus (``RemoteBackend``). Sin servicio se ejecutan en el propio proceso
(``LocalBackend``), con las mismas garantías del journal. Ambos lanzan las mismas
excepciones (``ActionError``, ``SuggestionError``…), así que quien llama no distingue.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from airgnizer.actions import ActionResult, ActionService
from airgnizer.app import (
    Accepted,
    AppContext,
    accept_suggestion,
    ignore_suggestion,
    load_model,
    move_and_learn,
    reload_settings,
    set_pattern,
    teach,
    undo_action,
)
from airgnizer.learning.model import DestinationModel

log = logging.getLogger(__name__)


class Backend(Protocol):
    remote: bool

    def move(
        self, source: Path, folder: Path, *, learn: bool = True
    ) -> tuple[ActionResult, str | None]: ...

    def rename(self, source: Path, new_name: str) -> ActionResult: ...

    def trash(self, source: Path, *, expected_hash: str | None = None) -> ActionResult: ...

    def undo(self, action_id: str) -> ActionResult:
        """Devuelve la acción de deshacer: ``destination`` es la ruta restaurada."""
        ...

    def accept(self, suggestion_id: int, folder: Path | None = None) -> Accepted: ...

    def ignore(self, suggestion_id: int) -> None: ...

    def teach(self, path: Path, folder: Path) -> str: ...

    def set_pattern(self, folder: str, template: str, enabled: bool) -> None: ...

    def reload_settings(self) -> None:
        """Aplica los cambios de ``config.toml`` (el servicio reinicia la vigilancia)."""
        ...


class LocalBackend:
    remote = False

    def __init__(
        self,
        ctx: AppContext,
        load: Callable[[AppContext], DestinationModel | None] = load_model,
        origin: str = "cli",
    ) -> None:
        self._ctx = ctx
        self._load = load
        self._origin = origin
        self._service: ActionService | None = None

    @property
    def actions(self) -> ActionService:
        if self._service is None:
            self._service = self._ctx.actions(origin=self._origin)
        return self._service

    def move(
        self, source: Path, folder: Path, *, learn: bool = True
    ) -> tuple[ActionResult, str | None]:
        return move_and_learn(self._ctx, self.actions, source, folder, learn=learn, load=self._load)

    def rename(self, source: Path, new_name: str) -> ActionResult:
        return self.actions.rename(source, new_name)

    def trash(self, source: Path, *, expected_hash: str | None = None) -> ActionResult:
        return self.actions.trash(source, expected_hash=expected_hash)

    def undo(self, action_id: str) -> ActionResult:
        undo_id = undo_action(self._ctx, action_id, actions=self.actions)
        [entry] = [e for e in self.actions.history(10) if e.id == undo_id]
        return ActionResult(undo_id, Path(entry.source), Path(entry.destination or ""))

    def accept(self, suggestion_id: int, folder: Path | None = None) -> Accepted:
        return accept_suggestion(self._ctx, suggestion_id, folder, actions=self.actions)

    def ignore(self, suggestion_id: int) -> None:
        ignore_suggestion(self._ctx, suggestion_id)

    def teach(self, path: Path, folder: Path) -> str:
        return teach(self._ctx, path, folder, self._load)

    def set_pattern(self, folder: str, template: str, enabled: bool) -> None:
        set_pattern(self._ctx, folder, template, enabled)

    def reload_settings(self) -> None:
        reload_settings(self._ctx)


def open_backend(
    ctx: AppContext,
    load: Callable[[AppContext], DestinationModel | None] = load_model,
    *,
    use_service: bool = True,
) -> Backend:
    """El servicio si está en marcha sobre la misma base de datos; si no, en local."""
    from airgnizer.service.client import RemoteBackend, ServiceClient  # noqa: PLC0415

    if use_service and (client := ServiceClient.connect(ctx.paths.database_file)) is not None:
        log.info("Operaciones delegadas en el servicio")
        return RemoteBackend(client)
    return LocalBackend(ctx, load)
