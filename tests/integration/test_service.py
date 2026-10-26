"""Servicio y API D-Bus contra un bus de sesión privado (sin tocar el de la sesión real)."""

from __future__ import annotations

import asyncio
import subprocess
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any

import pytest
from dbus_fast import Variant
from dbus_fast.aio import MessageBus, ProxyInterface
from dbus_fast.annotations import DBusSignature, DBusStr, DBusUInt32
from dbus_fast.errors import DBusError
from dbus_fast.service import ServiceInterface, method, signal

from organizador.actions import fsops
from organizador.app import AppContext
from organizador.core.config import LearningSettings, Settings, WatchSettings, save_settings
from organizador.core.paths import AppPaths
from organizador.service import systemd
from organizador.service.daemon import AlreadyRunningError, Daemon
from organizador.service.protocol import BUS_NAME, INTERFACE, OBJECT_PATH
from organizador.storage import Database
from organizador.storage.models import DownloadSuggestion

pytestmark = pytest.mark.integration

BUS_CONFIG = """<!DOCTYPE busconfig PUBLIC "-//freedesktop//DTD D-Bus Bus Configuration 1.0//EN"
 "http://www.freedesktop.org/standards/dbus/1.0/busconfig.dtd">
<busconfig>
  <type>session</type>
  <listen>unix:dir={dir}</listen>
  <auth>EXTERNAL</auth>
  <policy context="default">
    <allow send_destination="*" eavesdrop="true"/>
    <allow eavesdrop="true"/>
    <allow own="*"/>
  </policy>
</busconfig>
"""


@pytest.fixture
def bus_address(tmp_path: Path) -> Iterator[str]:
    config = tmp_path / "bus.conf"
    config.write_text(BUS_CONFIG.format(dir=tmp_path))
    proc = subprocess.Popen(
        ["dbus-daemon", f"--config-file={config}", "--nofork", "--print-address=1"],
        stdout=subprocess.PIPE,
    )
    assert proc.stdout is not None
    address = proc.stdout.readline().decode().strip()
    yield address
    proc.terminate()
    proc.wait(timeout=5)


@pytest.fixture
def home(isolated_home: Path) -> Path:
    (isolated_home / "Descargas").mkdir()
    (isolated_home / "Documentos/Banco").mkdir(parents=True)
    return isolated_home


def configure(home: Path, mode: str = "suggest") -> None:
    save_settings(
        AppPaths.from_env(),
        Settings(
            indexed_dirs=[home / "Documentos"],
            watched_dirs=[home / "Descargas"],
            watch=WatchSettings(mode=mode, stable_seconds=0.1),
            learning=LearningSettings(
                rules={"ext:pdf": str(home / "Documentos/Banco")}, type_destinations={}
            ),
        ),
    )


class FakeNotifications(ServiceInterface):
    """Servidor de notificaciones mínimo que registra lo que se muestra."""

    def __init__(self) -> None:
        super().__init__("org.freedesktop.Notifications")
        self.shown: list[tuple[int, str, list[str]]] = []
        self.closed: list[int] = []

    @method()
    def Notify(
        self,
        app_name: DBusStr,
        replaces_id: DBusUInt32,
        app_icon: DBusStr,
        summary: DBusStr,
        body: DBusStr,
        actions: Annotated[list[str], DBusSignature("as")],
        hints: Annotated[dict[str, Variant], DBusSignature("a{sv}")],
        expire_timeout: Annotated[int, DBusSignature("i")],
    ) -> DBusUInt32:
        nid = len(self.shown) + 1
        self.shown.append((nid, summary, actions))
        return nid

    @method()
    def CloseNotification(self, nid: DBusUInt32) -> Annotated[None, DBusSignature("")]:
        self.closed.append(nid)

    @signal()
    def ActionInvoked(self, nid: int, key: str) -> Annotated[list[Any], DBusSignature("us")]:
        return [nid, key]

    @signal()
    def NotificationClosed(
        self, nid: int, reason: int
    ) -> Annotated[list[Any], DBusSignature("uu")]:
        return [nid, reason]


async def eventually[T](
    probe: Callable[[], Awaitable[T]], ok: Callable[[T], bool], timeout: float = 5
) -> T:
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        value = await probe()
        if ok(value) or asyncio.get_running_loop().time() > deadline:
            return value
        await asyncio.sleep(0.05)


@asynccontextmanager
async def running_daemon(
    address: str, *, notify: bool = False
) -> AsyncIterator[tuple[ProxyInterface, Daemon]]:
    ctx = AppContext.open()
    daemon = Daemon(ctx, notify=notify, poll_seconds=0.05, rewatch_seconds=0.2)
    stop = asyncio.Event()
    task = asyncio.create_task(daemon.serve(stop, bus_address=address))
    client = await MessageBus(bus_address=address).connect()
    try:
        await eventually(lambda: _has_owner(client), bool)
        introspection = await client.introspect(BUS_NAME, OBJECT_PATH)
        proxy = client.get_proxy_object(BUS_NAME, OBJECT_PATH, introspection)
        yield proxy.get_interface(INTERFACE), daemon
    finally:
        stop.set()
        await task
        client.disconnect()
        ctx.db.dispose()


async def _has_owner(bus: MessageBus) -> bool:
    introspection = await bus.introspect("org.freedesktop.DBus", "/org/freedesktop/DBus")
    proxy = bus.get_proxy_object("org.freedesktop.DBus", "/org/freedesktop/DBus", introspection)
    dbus = proxy.get_interface("org.freedesktop.DBus")
    return bool(await dbus.call_name_has_owner(BUS_NAME))  # type: ignore[attr-defined]


def pending_of(api: ProxyInterface) -> Callable[[], Awaitable[list[dict[str, Variant]]]]:
    return api.call_list_pending  # type: ignore[attr-defined,no-any-return]


def test_list_accept_undo_ignore(home: Path, bus_address: str) -> None:
    configure(home)
    (home / "Descargas/cartola.pdf").write_bytes(b"%PDF-1.4")
    (home / "Descargas/raro.xyz").write_bytes(b"?")

    async def scenario() -> None:
        async with running_daemon(bus_address) as (api, _):
            assert await api.get_protocol_version() == 1  # type: ignore[attr-defined]
            assert await api.get_mode() == "suggest"  # type: ignore[attr-defined]
            added: list[dict[str, Variant]] = []
            api.on_suggestion_added(added.append)  # type: ignore[attr-defined]

            pending = await eventually(pending_of(api), lambda p: len(p) == 2)
            by_name = {Path(p["path"].value).name: p for p in pending}
            pdf = by_name["cartola.pdf"]
            assert pdf["destination"].value == str(home / "Documentos/Banco")
            assert pdf["available"].value is True

            action_id = await api.call_accept(pdf["id"].value, "")  # type: ignore[attr-defined]
            assert (home / "Documentos/Banco/cartola.pdf").exists()
            history = await api.call_history(10)  # type: ignore[attr-defined]
            assert history[0]["id"].value == action_id

            await api.call_undo(action_id[:8])  # type: ignore[attr-defined]
            assert (home / "Descargas/cartola.pdf").exists()
            pending = await api.call_list_pending()  # type: ignore[attr-defined]
            assert {Path(p["path"].value).name for p in pending} == {"cartola.pdf", "raro.xyz"}

            await api.call_ignore(by_name["raro.xyz"]["id"].value)  # type: ignore[attr-defined]
            pending = await api.call_list_pending()  # type: ignore[attr-defined]
            assert [Path(p["path"].value).name for p in pending] == ["cartola.pdf"]

            with pytest.raises(DBusError) as missing:
                await api.call_accept(999, "")  # type: ignore[attr-defined]
            assert missing.value.type == "io.github.organizador.Error.NotFound"
            with pytest.raises(DBusError) as resolved:
                await api.call_ignore(by_name["raro.xyz"]["id"].value)  # type: ignore[attr-defined]
            assert resolved.value.type == "io.github.organizador.Error.Invalid"
            with pytest.raises(DBusError) as no_target:
                await api.call_accept(by_name["raro.xyz"]["id"].value, "")  # type: ignore[attr-defined]
            assert no_target.value.type == "io.github.organizador.Error.Invalid"
            assert {Path(a["path"].value).name for a in added} <= {"cartola.pdf", "raro.xyz"}

    asyncio.run(scenario())


def test_notification_buttons_move_and_undo(home: Path, bus_address: str) -> None:
    configure(home, mode="auto")
    (home / "Descargas/sin_regla.txt").write_text("x")
    (home / "Descargas/cartola.pdf").write_bytes(b"%PDF-1.4")

    async def scenario() -> None:
        server_bus = await MessageBus(bus_address=bus_address).connect()
        server = FakeNotifications()
        server_bus.export("/org/freedesktop/Notifications", server)
        await server_bus.request_name("org.freedesktop.Notifications")
        try:
            async with running_daemon(bus_address, notify=True):

                async def shown() -> list[tuple[int, str, list[str]]]:
                    return server.shown

                await eventually(shown, lambda s: len(s) == 2)
                notes = {summary: (nid, actions) for nid, summary, actions in server.shown}
                # La regla del usuario mueve el pdf sola; la notificación ofrece deshacer.
                moved_id, moved_actions = notes["Movido: cartola.pdf"]
                assert moved_actions == ["undo", "Deshacer"]
                assert (home / "Documentos/Banco/cartola.pdf").exists()
                ask_id, ask_actions = notes["Descarga: sin_regla.txt"]
                assert "choose" in ask_actions and "ignore" in ask_actions

                server.ActionInvoked(moved_id, "undo")
                await eventually(lambda: _exists(home / "Descargas/cartola.pdf"), bool)
                server.ActionInvoked(ask_id, "ignore")
                await eventually(lambda: _exists(home / "Descargas/sin_regla.txt"), bool)
        finally:
            server_bus.disconnect()

    asyncio.run(scenario())
    db = Database(AppPaths.from_env().database_file)
    with db.session() as s:
        states = {Path(r.path).name: r.state for r in s.query(DownloadSuggestion)}
    db.dispose()
    assert states == {"cartola.pdf": "pending", "sin_regla.txt": "ignored"}


async def _exists(path: Path) -> bool:
    return path.exists()


def test_second_daemon_refuses_to_start(home: Path, bus_address: str) -> None:
    configure(home)

    async def scenario() -> None:
        async with running_daemon(bus_address):
            ctx = AppContext.open()
            with pytest.raises(AlreadyRunningError):
                await Daemon(ctx, notify=False).serve(asyncio.Event(), bus_address=bus_address)
            ctx.db.dispose()

    asyncio.run(scenario())


def test_restart_does_not_duplicate(home: Path, bus_address: str) -> None:
    configure(home)
    (home / "Descargas/a.pdf").write_bytes(b"1")

    async def scenario() -> None:
        async with running_daemon(bus_address) as (api, _):
            await eventually(pending_of(api), lambda p: len(p) == 1)
        async with running_daemon(bus_address) as (api, _):  # «Restart=on-failure»
            added: list[dict[str, Variant]] = []
            api.on_suggestion_added(added.append)  # type: ignore[attr-defined]
            await api.call_rescan()  # type: ignore[attr-defined]
            await asyncio.sleep(0.5)
            assert len(await api.call_list_pending()) == 1  # type: ignore[attr-defined]
            assert added == []

    asyncio.run(scenario())


def test_reader_and_daemon_at_once_without_lock_errors(home: Path, bus_address: str) -> None:
    """La GUI lee la base (y llama por D-Bus) mientras el servicio escribe."""
    configure(home)
    total = 60

    async def scenario() -> None:
        async with running_daemon(bus_address) as (api, _):
            reader = Database(AppPaths.from_env().database_file)  # otra conexión, como la GUI
            for i in range(total):
                (home / f"Descargas/archivo_{i}.pdf").write_bytes(str(i).encode())
            seen = 0
            deadline = asyncio.get_running_loop().time() + 15
            while seen < total and asyncio.get_running_loop().time() < deadline:
                with reader.session() as s:
                    seen = s.query(DownloadSuggestion).count()
                await api.call_list_pending()  # type: ignore[attr-defined]
            reader.dispose()
            pending = await api.call_list_pending()  # type: ignore[attr-defined]
            assert len(pending) == total
            ids = [p["id"].value for p in pending[:20]]
            # Aceptar en paralelo: el hilo escritor las serializa.
            await asyncio.gather(*(api.call_accept(i, "") for i in ids))  # type: ignore[attr-defined]
            assert len(list((home / "Documentos/Banco").iterdir())) == 20

    asyncio.run(scenario())


def test_systemd_unit_and_activation(isolated_home: Path) -> None:
    text = systemd.unit_text(["/usr/bin/organizador", "daemon"])
    assert "Type=dbus" in text and f"BusName={BUS_NAME}" in text
    assert "Restart=on-failure" in text
    assert "ExecStart=/usr/bin/organizador daemon" in text

    files = systemd.install(["/usr/bin/python", "-m", "organizador", "daemon"])
    assert files == [
        isolated_home / ".config/systemd/user/organizador.service",
        isolated_home / f".local/share/dbus-1/services/{BUS_NAME}.service",
    ]
    assert "SystemdService=organizador.service" in files[1].read_text()
    for path in systemd.installed_files():
        fsops.remove_service_file(path)
    assert systemd.installed_files() == []

    other = isolated_home / ".config/systemd/user/otro.service"
    other.write_text("x")
    with pytest.raises(ValueError, match="servicio"):
        fsops.remove_service_file(other)
    assert other.exists()
