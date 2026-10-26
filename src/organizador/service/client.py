"""Cliente de la API D-Bus del servicio, utilizable desde código síncrono.

El bus se atiende en un bucle asyncio propio, en un hilo aparte: la CLI llama y espera,
y la GUI puede llamar desde sus hilos de trabajo y recibir señales sin bloquear Qt.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from collections.abc import Callable, Coroutine
from concurrent.futures import Future
from pathlib import Path
from typing import Any

from dbus_fast import Variant
from dbus_fast.aio import MessageBus, ProxyInterface
from dbus_fast.errors import DBusError

from organizador.actions import ActionError, ActionResult
from organizador.app import Accepted, SuggestionError, SuggestionMissingError
from organizador.service.protocol import (
    BUS_NAME,
    ERROR_INVALID,
    ERROR_NOT_FOUND,
    INTERFACE,
    OBJECT_PATH,
)

log = logging.getLogger(__name__)

CONNECT_TIMEOUT = 3.0


class ServiceClient:
    """Conexión al servicio. Usa ``connect`` para obtenerla."""

    def __init__(self, loop: asyncio.AbstractEventLoop, thread: threading.Thread) -> None:
        self._loop = loop
        self._thread = thread
        self._bus: MessageBus | None = None
        self._api: ProxyInterface | None = None

    # --- conexión --------------------------------------------------------------------

    @classmethod
    def connect(cls, database_file: Path, bus_address: str | None = None) -> ServiceClient | None:
        """Cliente del servicio que usa ``database_file``; ``None`` si no hay ninguno.

        No arranca el servicio: si no está en marcha, el llamador trabaja en local.
        """
        if bus_address is None and not os.environ.get("DBUS_SESSION_BUS_ADDRESS"):
            return None
        loop = asyncio.new_event_loop()
        thread = threading.Thread(target=loop.run_forever, name="dbus-cliente", daemon=True)
        thread.start()
        client = cls(loop, thread)
        try:
            found = client._submit(client._attach(bus_address)).result(CONNECT_TIMEOUT)
        except (DBusError, OSError, TimeoutError, ValueError) as exc:
            log.debug("Sin servicio en el bus: %s", exc)
            found = None
        if found is None:
            client.close()
            return None
        if Path(found).resolve() != database_file.resolve():
            # Otro servicio (otro HOME, una prueba): no es el escritor de nuestra base.
            log.info("El servicio en marcha usa otra base (%s); se trabaja en local", found)
            client.close()
            return None
        return client

    async def _attach(self, bus_address: str | None) -> str | None:
        bus = await MessageBus(bus_address=bus_address).connect()
        self._bus = bus
        introspection = await bus.introspect("org.freedesktop.DBus", "/org/freedesktop/DBus")
        dbus = bus.get_proxy_object(
            "org.freedesktop.DBus", "/org/freedesktop/DBus", introspection
        ).get_interface("org.freedesktop.DBus")
        if not await dbus.call_name_has_owner(BUS_NAME):  # type: ignore[attr-defined]
            return None
        introspection = await bus.introspect(BUS_NAME, OBJECT_PATH)
        self._api = bus.get_proxy_object(BUS_NAME, OBJECT_PATH, introspection).get_interface(
            INTERFACE
        )
        return str(await self._api.get_database_path())  # type: ignore[attr-defined]

    def close(self) -> None:
        if self._bus is not None:
            self._loop.call_soon_threadsafe(self._bus.disconnect)
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=2)

    # --- llamadas --------------------------------------------------------------------

    def _submit[T](self, coro: Coroutine[Any, Any, T]) -> Future[T]:
        return asyncio.run_coroutine_threadsafe(coro, self._loop)

    def call(self, member: str, *args: Any) -> Any:
        """Llama a un método (``"ListPending"``…) y espera; traduce los errores D-Bus."""
        assert self._api is not None
        method = getattr(self._api, "call_" + _snake(member))
        try:
            return self._submit(method(*args)).result()
        except DBusError as exc:
            raise _local_error(exc) from exc

    def on(self, signal: str, callback: Callable[..., None]) -> None:
        """Suscribe ``callback`` a una señal; se llama desde el hilo del bus."""
        assert self._api is not None
        register = getattr(self._api, "on_" + _snake(signal))
        self._loop.call_soon_threadsafe(register, callback)


def _snake(name: str) -> str:
    return "".join(f"_{c.lower()}" if c.isupper() else c for c in name).lstrip("_")


def _local_error(exc: DBusError) -> Exception:
    message = exc.text or str(exc)
    if exc.type == ERROR_NOT_FOUND:
        return SuggestionMissingError(message)
    if exc.type == ERROR_INVALID:
        return SuggestionError(message)
    return ActionError(message)


def plain(data: dict[str, Variant]) -> dict[str, Any]:
    return {key: value.value for key, value in data.items()}


class RemoteBackend:
    """``Backend`` que pide cada operación al servicio."""

    remote = True

    def __init__(self, client: ServiceClient) -> None:
        self.client = client

    def _result(self, data: dict[str, Variant]) -> ActionResult:
        d = plain(data)
        return ActionResult(d["action_id"], Path(d["source"]), Path(d["destination"]))

    def move(
        self, source: Path, folder: Path, *, learn: bool = True
    ) -> tuple[ActionResult, str | None]:
        data = self.client.call(
            "Move", str(source.expanduser().absolute()), str(folder.expanduser().absolute()), learn
        )
        return self._result(data), plain(data)["learned"] or None

    def rename(self, source: Path, new_name: str) -> ActionResult:
        return self._result(self.client.call("Rename", str(source.absolute()), new_name))

    def trash(self, source: Path, *, expected_hash: str | None = None) -> ActionResult:
        return self._result(self.client.call("Trash", str(source.absolute()), expected_hash or ""))

    def _entry(self, action_id: str) -> dict[str, Any]:
        for raw in self.client.call("History", 20):
            entry = plain(raw)
            if entry["id"] == action_id:
                return entry
        raise ActionError(f"El servicio no informó de la acción {action_id}")

    def undo(self, action_id: str) -> ActionResult:
        undo_id = self.client.call("Undo", action_id)
        entry = self._entry(undo_id)
        return ActionResult(undo_id, Path(entry["source"]), Path(entry["destination"]))

    def accept(self, suggestion_id: int, folder: Path | None = None) -> Accepted:
        target = str(folder.expanduser().absolute()) if folder else ""
        action_id = self.client.call("Accept", suggestion_id, target)
        entry = self._entry(action_id)
        return Accepted(
            suggestion_id,
            action_id,
            Path(entry["source"]),
            Path(entry["destination"]),
            "aprendido por el servicio",
        )

    def ignore(self, suggestion_id: int) -> None:
        self.client.call("Ignore", suggestion_id)

    def teach(self, path: Path, folder: Path) -> str:
        return str(
            self.client.call(
                "Teach", str(path.expanduser().absolute()), str(folder.expanduser().absolute())
            )
        )
