"""Protocolo D-Bus del servicio, versión 1.

Bus de sesión, nombre ``io.github.organizador.Organizador``, objeto
``/io/github/organizador/Organizador``, interfaz ``io.github.organizador.Organizador1``.
Un cambio incompatible crea ``…Organizador2`` y sube ``ProtocolVersion``; los añadidos
compatibles (métodos o claves nuevas en los diccionarios) no cambian la versión.

Propiedades (lectura)
    ``ProtocolVersion u`` · ``Mode s`` («suggest» | «auto») · ``WatchedDirs as`` ·
    ``DatabasePath s`` (los clientes solo delegan si coincide con la suya)

Métodos
    ``ListPending() → aa{sv}``         sugerencias pendientes (ver claves abajo)
    ``Accept(x id, s folder) → s``     mueve a ``folder`` (vacío: el sugerido) y aprende;
                                       devuelve el id de la acción en el journal
    ``Ignore(x id)``                   el archivo se queda donde está
    ``Undo(s action_id) → s``          deshace una acción (id o prefijo); devuelve el id
                                       de la acción de deshacer
    ``Move(s source, s folder, b learn) → a{sv}``  mueve (crea la carpeta) y, con
                                       ``learn``, aprende la elección
    ``Rename(s source, s new_name) → a{sv}``
    ``Trash(s source, s expected_hash) → a{sv}``   papelera; con hash (duplicados) se
                                       rechaza si el contenido ya no coincide
    ``Teach(s path, s folder) → s``    corrige al modelo sin mover; devuelve qué aprendió
    ``History(u limit) → aa{sv}``      últimas acciones
    ``Rescan()``                       relee ahora las carpetas vigiladas

Señales
    ``SuggestionAdded(a{sv})``                      nueva sugerencia (también si el modo
                                                    automático ya la movió: ``state=auto``)
    ``SuggestionResolved(x id, s state, s action)`` aceptada, ignorada o caducada

Sugerencia ``a{sv}``: ``id x``, ``path s``, ``destination s`` (vacío si no hay),
``confidence d``, ``source s``, ``alternatives as``, ``state s``, ``available b``
(falso si su disco está desconectado), ``action_id s``.

Resultado ``a{sv}`` (Move, Rename, Trash): ``action_id s``, ``source s``,
``destination s``, ``learned s`` (vacío si no aprendió).

Acción ``a{sv}``: ``id s``, ``kind s``, ``state s``, ``source s``, ``destination s``,
``origin s``, ``created_at s`` (ISO 8601).

Errores: ``io.github.organizador.Error.NotFound``, ``….Invalid``, ``….Action``.
"""

from __future__ import annotations

BUS_NAME = "io.github.organizador.Organizador"
OBJECT_PATH = "/io/github/organizador/Organizador"
INTERFACE = "io.github.organizador.Organizador1"
PROTOCOL_VERSION = 1

ERROR_NOT_FOUND = "io.github.organizador.Error.NotFound"
ERROR_INVALID = "io.github.organizador.Error.Invalid"
ERROR_ACTION = "io.github.organizador.Error.Action"
