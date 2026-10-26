"""Interfaz D-Bus exportada por el servicio (ver ``protocol.py``).

dbus-fast lee las firmas D-Bus de las anotaciones ``Annotated[tipo, DBusSignature]``;
los nombres de métodos y señales siguen la convención CamelCase de D-Bus.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any

from dbus_fast import Variant
from dbus_fast.annotations import DBusBool, DBusInt64, DBusSignature, DBusStr, DBusUInt32
from dbus_fast.constants import PropertyAccess
from dbus_fast.errors import DBusError
from dbus_fast.service import ServiceInterface, dbus_property, method, signal

from organizador.service.protocol import INTERFACE, PROTOCOL_VERSION

if TYPE_CHECKING:
    from organizador.service.daemon import Daemon


VariantDict = dict[str, Variant]
DBusDict = Annotated[VariantDict, DBusSignature("a{sv}")]
DBusDictList = Annotated[list[VariantDict], DBusSignature("aa{sv}")]
DBusStrList = Annotated[list[str], DBusSignature("as")]
DBusResolved = Annotated[list[Any], DBusSignature("xss")]
# Sin valor de retorno: dbus-fast no acepta ``-> None``.
NoReply = Annotated[None, DBusSignature("")]


class OrganizadorInterface(ServiceInterface):
    def __init__(self, daemon: Daemon) -> None:
        super().__init__(INTERFACE)
        self._daemon = daemon

    # --- propiedades -----------------------------------------------------------------

    @dbus_property(access=PropertyAccess.READ)
    def ProtocolVersion(self) -> DBusUInt32:
        return PROTOCOL_VERSION

    @dbus_property(access=PropertyAccess.READ)
    def Mode(self) -> DBusStr:
        return self._daemon.mode

    @dbus_property(access=PropertyAccess.READ)
    def DatabasePath(self) -> DBusStr:
        return str(self._daemon.database_path)

    @dbus_property(access=PropertyAccess.READ)
    def WatchedDirs(self) -> DBusStrList:
        return [str(d) for d in self._daemon.watched_dirs]

    # --- métodos ---------------------------------------------------------------------

    @method()
    async def ListPending(self) -> DBusDictList:
        return await self._daemon.list_pending()

    @method()
    async def Accept(self, suggestion_id: DBusInt64, folder: DBusStr) -> DBusStr:
        return await self._daemon.accept(suggestion_id, folder or None)

    @method()
    async def Ignore(self, suggestion_id: DBusInt64) -> NoReply:
        await self._daemon.ignore(suggestion_id)

    @method()
    async def Undo(self, action_id: DBusStr) -> DBusStr:
        return await self._daemon.undo(action_id)

    @method()
    async def Move(self, source: DBusStr, folder: DBusStr, learn: DBusBool) -> DBusDict:
        return await self._daemon.move(source, folder, learn)

    @method()
    async def Rename(self, source: DBusStr, new_name: DBusStr) -> DBusDict:
        return await self._daemon.rename(source, new_name)

    @method()
    async def Trash(self, source: DBusStr, expected_hash: DBusStr) -> DBusDict:
        return await self._daemon.trash(source, expected_hash)

    @method()
    async def Teach(self, path: DBusStr, folder: DBusStr) -> DBusStr:
        return await self._daemon.teach(path, folder)

    @method()
    async def History(self, limit: DBusUInt32) -> DBusDictList:
        return await self._daemon.history(limit)

    @method()
    async def Rescan(self) -> NoReply:
        await self._daemon.rescan()

    # --- señales (el valor devuelto es lo que se emite) -------------------------------

    @signal()
    def SuggestionAdded(self, suggestion: VariantDict) -> DBusDict:
        return suggestion

    @signal()
    def SuggestionResolved(self, suggestion_id: int, state: str, action_id: str) -> DBusResolved:
        return [suggestion_id, state, action_id]


def error(name: str, message: str) -> DBusError:
    return DBusError(name, message)


def variants(data: dict[str, Any]) -> dict[str, Variant]:
    """Diccionario Python → ``a{sv}`` (tipos: bool, int, float, str, list[str])."""
    out: dict[str, Variant] = {}
    for key, value in data.items():
        if isinstance(value, bool):
            out[key] = Variant("b", value)
        elif isinstance(value, int):
            out[key] = Variant("x", value)
        elif isinstance(value, float):
            out[key] = Variant("d", value)
        elif isinstance(value, list):
            out[key] = Variant("as", [str(v) for v in value])
        else:
            out[key] = Variant("s", "" if value is None else str(value))
    return out
