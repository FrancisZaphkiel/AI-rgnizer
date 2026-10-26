"""Servicio de usuario: vigila las descargas, notifica y atiende la API D-Bus.

Es el **único escritor** de la base de datos mientras está en marcha: la GUI y las
herramientas le piden las operaciones por D-Bus. Dentro del proceso todo lo que escribe
(ticks del monitor y llamadas D-Bus) se ejecuta en un único hilo, en orden, así que no
hay escrituras concurrentes ni ``database is locked``; el bucle de asyncio solo atiende
el bus.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from pathlib import Path
from typing import Any

from dbus_fast import BusType, Variant
from dbus_fast.aio import MessageBus
from dbus_fast.constants import NameFlag, RequestNameReply

from organizador.actions import ActionError, ActionResult, ActionService
from organizador.actions.errors import ConcurrentOperationError
from organizador.actions.locks import process_lock
from organizador.app import (
    AppContext,
    SuggestionError,
    SuggestionMissingError,
    accept_suggestion,
    classifier,
    ignore_suggestion,
    undo_action,
)
from organizador.backend import LocalBackend
from organizador.downloads import DownloadMonitor, Outcome, SuggestionQueue
from organizador.downloads.queue import alternatives_of, available
from organizador.service.api import OrganizadorInterface, error, variants
from organizador.service.notifications import Notifier, Pending, choose_folder
from organizador.service.protocol import (
    BUS_NAME,
    ERROR_ACTION,
    ERROR_INVALID,
    ERROR_NOT_FOUND,
    OBJECT_PATH,
)
from organizador.storage.models import ActionJournal, DownloadSuggestion, SuggestionState

log = logging.getLogger(__name__)


class AlreadyRunningError(RuntimeError):
    pass


def suggestion_dict(row: DownloadSuggestion) -> dict[str, Variant]:
    return variants(
        {
            "id": row.id,
            "path": row.path,
            "destination": row.destination,
            "confidence": float(row.confidence or 0.0),
            "source": row.source,
            "alternatives": [label for label, _ in alternatives_of(row)],
            "state": str(row.state),
            "available": available(row),
            "action_id": row.action_id,
        }
    )


def result_dict(result: ActionResult, learned: str | None = None) -> dict[str, Variant]:
    return variants(
        {
            "action_id": result.action_id,
            "source": str(result.source),
            "destination": str(result.destination),
            "learned": learned or "",
        }
    )


def action_dict(entry: ActionJournal) -> dict[str, Variant]:
    return variants(
        {
            "id": entry.id,
            "kind": str(entry.kind),
            "state": str(entry.state),
            "source": entry.source,
            "destination": entry.destination,
            "origin": entry.origin,
            "created_at": entry.created_at.isoformat(),
        }
    )


class Daemon:
    def __init__(
        self,
        ctx: AppContext,
        *,
        notify: bool = True,
        poll_seconds: float = 0.5,
        rewatch_seconds: float = 5.0,
    ) -> None:
        self._ctx = ctx
        self._notify = notify
        self._poll_seconds = poll_seconds
        self._rewatch_seconds = rewatch_seconds
        self._writer = ThreadPoolExecutor(max_workers=1, thread_name_prefix="escritor")
        self._queue = SuggestionQueue(ctx.db)
        self.mode = ctx.settings.watch.mode
        self.database_path = ctx.paths.database_file
        self.watched_dirs = [d.expanduser() for d in ctx.settings.watched_dirs]
        self._actions: ActionService | None = None
        self._local: LocalBackend | None = None
        self._monitor: DownloadMonitor | None = None
        self._iface = OrganizadorInterface(self)
        self._notifier: Notifier | None = None
        self._bus: MessageBus | None = None

    # --- ciclo de vida -----------------------------------------------------------------

    async def serve(self, stop: asyncio.Event, bus_address: str | None = None) -> None:
        """Atiende hasta que se active ``stop``. Lanza ``AlreadyRunningError`` si ya hay otro."""
        instance = contextlib.ExitStack()
        try:
            instance.enter_context(
                process_lock(self._ctx.paths.state_dir / "daemon.lock", timeout=0)
            )
        except ConcurrentOperationError as exc:
            raise AlreadyRunningError("Ya hay un servicio en marcha para este usuario") from exc
        with instance:
            await self._serve(stop, bus_address)

    async def _serve(self, stop: asyncio.Event, bus_address: str | None) -> None:
        bus = await MessageBus(bus_address=bus_address, bus_type=BusType.SESSION).connect()
        self._bus = bus
        try:
            if self._notify:
                notifier = Notifier(bus, self._on_notification_action)
                if await notifier.connect():
                    self._notifier = notifier
            await self._write(self._start)
            bus.export(OBJECT_PATH, self._iface)
            # El nombre se toma al final: quien lo vea (o systemd, con Type=dbus) puede
            # llamar ya a cualquier método.
            reply = await bus.request_name(BUS_NAME, NameFlag.DO_NOT_QUEUE)
            if reply not in (RequestNameReply.PRIMARY_OWNER, RequestNameReply.ALREADY_OWNER):
                await self._write(self._close)
                raise AlreadyRunningError(f"Ya hay un servicio con el nombre {BUS_NAME}")
            dirs = ", ".join(map(str, self.watched_dirs))
            log.info("Servicio en marcha (%s): %s", self.mode, dirs)
            try:
                await self._loop(stop)
            finally:
                await self._write(self._close)
        finally:
            bus.disconnect()
            self._writer.shutdown(wait=True)

    def _start(self) -> None:
        self._local = LocalBackend(self._ctx, origin="servicio")
        actions = self._actions = self._local.actions

        def mover(source: Path, folder: Path) -> str:
            return actions.move(source, folder).action_id

        monitor = DownloadMonitor(
            self.watched_dirs, self._ctx.settings.watch, self._queue, classifier(self._ctx), mover
        )
        monitor.start(rewatch_seconds=self._rewatch_seconds)
        self._monitor = monitor

    def _close(self) -> None:
        if self._monitor is not None:
            self._monitor.close()

    async def _loop(self, stop: asyncio.Event) -> None:
        assert self._monitor is not None
        while not stop.is_set():
            for outcome in await self._write(self._monitor.tick):
                await self._announce(outcome)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), self._poll_seconds)

    async def _write[T](self, fn: Callable[..., T], *args: Any) -> T:
        """Ejecuta ``fn`` en el hilo escritor (todas las escrituras, en orden)."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._writer, partial(fn, *args))

    async def _announce(self, outcome: Outcome) -> None:
        row = outcome.suggestion
        self._iface.SuggestionAdded(suggestion_dict(row))
        if self._notifier is None:
            return
        if outcome.moved_to is not None and row.action_id:
            await self._notifier.moved(row.id, Path(row.path), str(outcome.moved_to), row.action_id)
        else:
            await self._notifier.suggestion(row.id, Path(row.path), row.destination, row.confidence)

    # --- API (la usan api.py y las notificaciones) -------------------------------------

    async def list_pending(self) -> list[dict[str, Variant]]:
        def read() -> list[dict[str, Variant]]:
            self._queue.expire_missing()
            return [suggestion_dict(r) for r in self._queue.pending()]

        return await self._write(read)

    async def accept(self, suggestion_id: int, folder: str | None) -> str:
        target = Path(folder) if folder else None
        try:
            accepted = await self._write(
                partial(accept_suggestion, actions=self._actions),
                self._ctx,
                suggestion_id,
                target,
            )
        except (SuggestionError, ActionError) as exc:
            raise _dbus_error(exc) from exc
        log.info("Aceptada %d: %s → %s", suggestion_id, accepted.source, accepted.destination)
        await self._resolved(suggestion_id, SuggestionState.ACCEPTED, accepted.action_id)
        return accepted.action_id

    async def ignore(self, suggestion_id: int) -> None:
        try:
            await self._write(ignore_suggestion, self._ctx, suggestion_id)
        except SuggestionError as exc:
            raise _dbus_error(exc) from exc
        await self._resolved(suggestion_id, SuggestionState.IGNORED, "")

    async def undo(self, action_id: str) -> str:
        try:
            return await self._write(
                partial(undo_action, actions=self._actions), self._ctx, action_id
            )
        except ActionError as exc:
            raise _dbus_error(exc) from exc

    async def _op[T](self, fn: Callable[..., T], *args: Any) -> T:
        """Operación del backend en el hilo escritor, con los errores traducidos a D-Bus."""
        try:
            return await self._write(fn, *args)
        except (SuggestionError, ActionError, ValueError, OSError) as exc:
            raise _dbus_error(exc) from exc

    def _backend(self) -> LocalBackend:
        assert self._local is not None
        return self._local

    async def move(self, source: str, folder: str, learn: bool) -> dict[str, Variant]:
        result, learned = await self._op(
            partial(self._backend().move, learn=learn), Path(source), Path(folder)
        )
        return result_dict(result, learned)

    async def rename(self, source: str, new_name: str) -> dict[str, Variant]:
        return result_dict(await self._op(self._backend().rename, Path(source), new_name))

    async def trash(self, source: str, expected_hash: str) -> dict[str, Variant]:
        result = await self._op(
            partial(self._backend().trash, expected_hash=expected_hash or None), Path(source)
        )
        return result_dict(result)

    async def teach(self, path: str, folder: str) -> str:
        return await self._op(self._backend().teach, Path(path), Path(folder))

    async def set_pattern(self, folder: str, template: str, enabled: bool) -> None:
        await self._op(self._backend().set_pattern, folder, template, enabled)

    async def reload_settings(self) -> None:
        """Relee ``config.toml`` y reinicia la vigilancia con las carpetas nuevas."""

        def reload() -> None:
            self._close()
            self._backend().reload_settings()
            self.mode = self._ctx.settings.watch.mode
            self.watched_dirs = [d.expanduser() for d in self._ctx.settings.watched_dirs]
            self._start()

        await self._write(reload)
        log.info("Configuración recargada (%s): %s", self.mode, self.watched_dirs)

    async def history(self, limit: int) -> list[dict[str, Variant]]:
        def read() -> list[dict[str, Variant]]:
            assert self._actions is not None
            return [action_dict(e) for e in self._actions.history(limit or 50)]

        return await self._write(read)

    async def rescan(self) -> None:
        assert self._monitor is not None
        await self._write(self._monitor.reconcile)

    async def _resolved(self, suggestion_id: int, state: SuggestionState, action_id: str) -> None:
        self._iface.SuggestionResolved(suggestion_id, str(state), action_id)
        if self._notifier is not None:
            await self._notifier.withdraw(suggestion_id)

    # --- botones de las notificaciones -------------------------------------------------

    async def _on_notification_action(self, pending: Pending, key: str) -> None:
        try:
            if pending.kind == "moved":
                if key == "undo":
                    await self.undo(pending.action_id)
                return
            if key == "ignore":
                await self.ignore(pending.suggestion_id)
                return
            row = await self._write(self._queue.get, pending.suggestion_id)
            if key in {"move", "default"} and row.destination:
                await self.accept(pending.suggestion_id, None)
            elif key in {"choose", "default"} and self._bus is not None:
                folder = await choose_folder(self._bus, f"¿Dónde va {Path(row.path).name}?")
                if folder is not None:
                    await self.accept(pending.suggestion_id, str(folder))
        except Exception:
            # Un botón no debe tumbar el servicio; el error queda en el registro.
            log.exception("No se pudo atender «%s» de la notificación", key)


def _dbus_error(exc: Exception) -> Exception:
    if isinstance(exc, SuggestionMissingError):
        return error(ERROR_NOT_FOUND, str(exc))
    if isinstance(exc, SuggestionError | ValueError):
        return error(ERROR_INVALID, str(exc))
    return error(ERROR_ACTION, str(exc))
