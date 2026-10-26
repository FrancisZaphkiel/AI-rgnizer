"""Notificaciones de escritorio con acciones (org.freedesktop.Notifications).

En KDE Plasma (Wayland) los botones aparecen en la notificación y quedan en el historial.
«Elegir…» abre el selector de carpetas del portal de escritorio
(``org.freedesktop.portal.FileChooser``), que en Plasma es el diálogo nativo de KDE; si
el portal no está, se prueba con ``kdialog`` y ``zenity``.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import shutil
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from dbus_fast import Message, MessageType, Variant
from dbus_fast.aio import MessageBus, ProxyInterface
from dbus_fast.errors import DBusError

log = logging.getLogger(__name__)

NOTIFICATIONS_NAME = "org.freedesktop.Notifications"
NOTIFICATIONS_PATH = "/org/freedesktop/Notifications"
PORTAL_NAME = "org.freedesktop.portal.Desktop"
PORTAL_PATH = "/org/freedesktop/portal/desktop"
FILE_CHOOSER = "org.freedesktop.portal.FileChooser"
APP_NAME = "AI-rgnizer"
ICON = "io.github.airgnizer.AIrgnizer"  # instalado por desktop.py; si no, el genérico


@dataclass(frozen=True, slots=True)
class Pending:
    """Lo que representa una notificación abierta."""

    kind: str  # "suggestion" | "moved"
    suggestion_id: int
    action_id: str = ""


# (pending, clave de la acción pulsada) → corrutina que la atiende
ActionHandler = Callable[[Pending, str], Coroutine[Any, Any, None]]


class Notifier:
    def __init__(self, bus: MessageBus, on_action: ActionHandler) -> None:
        self._bus = bus
        self._on_action = on_action
        self._iface: ProxyInterface | None = None
        self._open: dict[int, Pending] = {}
        self._tasks: set[asyncio.Task[None]] = set()

    async def connect(self) -> bool:
        """Conecta con el servidor de notificaciones; ``False`` si no hay ninguno."""
        try:
            introspection = await self._bus.introspect(NOTIFICATIONS_NAME, NOTIFICATIONS_PATH)
        except (DBusError, OSError) as exc:
            log.warning("Sin servidor de notificaciones: %s", exc)
            return False
        proxy = self._bus.get_proxy_object(NOTIFICATIONS_NAME, NOTIFICATIONS_PATH, introspection)
        self._iface = proxy.get_interface(NOTIFICATIONS_NAME)
        self._iface.on_action_invoked(self._action_invoked)  # type: ignore[attr-defined]
        self._iface.on_notification_closed(self._closed)  # type: ignore[attr-defined]
        return True

    async def suggestion(
        self, suggestion_id: int, path: Path, destination: str | None, confidence: float | None
    ) -> None:
        if destination:
            shown = f" ({confidence:.0%})" if confidence else ""
            body = f"¿Mover a <b>{_escape(destination)}</b>{shown}?"
            actions = ["default", "Mover", "move", f"Mover a {Path(destination).name}"]
        else:
            body = "No sé dónde va: elige una carpeta y lo aprenderé."
            actions = ["default", "Elegir…"]
        actions += ["choose", "Elegir…", "ignore", "Ignorar"]
        await self._notify(
            f"Descarga: {path.name}", body, actions, Pending("suggestion", suggestion_id)
        )

    async def moved(self, suggestion_id: int, path: Path, destination: str, action_id: str) -> None:
        await self._notify(
            f"Movido: {path.name}",
            f"a <b>{_escape(destination)}</b>",
            ["undo", "Deshacer"],
            Pending("moved", suggestion_id, action_id),
        )

    async def withdraw(self, suggestion_id: int) -> None:
        """Cierra la notificación de una sugerencia resuelta por otra vía (CLI, GUI)."""
        if self._iface is None:
            return
        for nid, pending in list(self._open.items()):
            if pending.suggestion_id == suggestion_id and pending.kind == "suggestion":
                self._open.pop(nid, None)
                try:
                    await self._iface.call_close_notification(nid)  # type: ignore[attr-defined]
                except DBusError as exc:
                    log.debug("No se pudo cerrar la notificación %d: %s", nid, exc)

    async def _notify(self, summary: str, body: str, actions: list[str], pending: Pending) -> None:
        if self._iface is None:
            return
        hints = {
            "category": Variant("s", "transfer.complete"),
            "resident": Variant("b", True),
        }
        try:
            nid = await self._iface.call_notify(  # type: ignore[attr-defined]
                APP_NAME, 0, ICON, summary, body, actions, hints, -1
            )
        except DBusError as exc:
            log.warning("No se pudo mostrar la notificación: %s", exc)
            return
        self._open[int(nid)] = pending

    def _action_invoked(self, nid: int, key: str) -> None:
        pending = self._open.get(nid)
        if pending is None:
            return  # de otra aplicación
        if pending.kind == "suggestion":
            self._open.pop(nid, None)
        task = asyncio.get_running_loop().create_task(self._on_action(pending, key))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _closed(self, nid: int, _reason: int) -> None:
        self._open.pop(nid, None)


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# --- selector de carpetas ---------------------------------------------------------------------


async def choose_folder(bus: MessageBus, title: str) -> Path | None:
    """Pide una carpeta al usuario; ``None`` si cancela o no hay forma de preguntar."""
    try:
        return await _portal_folder(bus, title)
    except (DBusError, OSError, TimeoutError) as exc:
        log.info("Portal de archivos no disponible (%s); se prueba kdialog/zenity", exc)
    return await _dialog_folder(title)


async def _portal_folder(bus: MessageBus, title: str) -> Path | None:
    token = "airgnizer" + secrets.token_hex(4)
    sender = (bus.unique_name or "").lstrip(":").replace(".", "_")
    request_path = f"{PORTAL_PATH}/request/{sender}/{token}"
    loop = asyncio.get_running_loop()
    answer: asyncio.Future[tuple[int, dict[str, Variant]]] = loop.create_future()

    def on_message(message: Message) -> None:
        if (
            message.message_type == MessageType.SIGNAL
            and message.path == request_path
            and message.member == "Response"
            and not answer.done()
        ):
            answer.set_result((message.body[0], message.body[1]))

    # Suscribirse antes de llamar: la respuesta puede llegar antes que el retorno.
    rule = f"type='signal',interface='org.freedesktop.portal.Request',path='{request_path}'"
    await _call(
        bus,
        Message(
            destination="org.freedesktop.DBus",
            path="/org/freedesktop/DBus",
            interface="org.freedesktop.DBus",
            member="AddMatch",
            signature="s",
            body=[rule],
        ),
    )
    bus.add_message_handler(on_message)
    try:
        options = {
            "handle_token": Variant("s", token),
            "directory": Variant("b", True),
            "modal": Variant("b", True),
        }
        await _call(
            bus,
            Message(
                destination=PORTAL_NAME,
                path=PORTAL_PATH,
                interface=FILE_CHOOSER,
                member="OpenFile",
                signature="ssa{sv}",
                body=["", title, options],
            ),
        )
        response, results = await asyncio.wait_for(answer, timeout=600)
    finally:
        bus.remove_message_handler(on_message)
    if response != 0:
        return None  # cancelado
    uris = results.get("uris")
    if uris is None or not uris.value:
        return None
    return Path(unquote(urlsplit(uris.value[0]).path))


async def _call(bus: MessageBus, message: Message) -> Message:
    reply = await bus.call(message)
    if reply is None or reply.message_type == MessageType.ERROR:
        detail = reply.body[0] if reply is not None and reply.body else "sin respuesta"
        name = (reply.error_name if reply else None) or "org.freedesktop.DBus.Error.NoReply"
        raise DBusError(name, detail)
    return reply


async def _dialog_folder(title: str) -> Path | None:
    for argv in (
        ["kdialog", "--title", title, "--getexistingdirectory", str(Path.home())],
        ["zenity", "--file-selection", "--directory", f"--title={title}"],
    ):
        if shutil.which(argv[0]) is None:
            continue
        proc = await asyncio.create_subprocess_exec(*argv, stdout=asyncio.subprocess.PIPE)
        out, _ = await proc.communicate()
        if proc.returncode != 0:
            return None  # cancelado
        chosen = out.decode().strip()
        return Path(chosen) if chosen else None
    log.warning("No hay forma de preguntar una carpeta (ni portal, ni kdialog, ni zenity)")
    return None
