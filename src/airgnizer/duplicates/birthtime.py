"""Fecha de creación (``btime``) de un archivo mediante ``statx(2)``.

Python no la expone en Linux (``st_birthtime`` no existe), pero Btrfs, ext4, XFS y
otros la guardan. A diferencia de ``mtime``, que ``cp -p`` o Dolphin conservan al copiar,
``btime`` siempre es la del momento en que se creó esa copia concreta.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import os
import struct
from collections.abc import Callable
from pathlib import Path

_AT_FDCWD = -100
_AT_SYMLINK_NOFOLLOW = 0x100
_STATX_BTIME = 0x800
_STATX_SIZE = 256  # tamaño de struct statx (incluye relleno reservado)
# stx_mask está en el desplazamiento 0; stx_btime (tv_sec i64, tv_nsec u32) en el 80.
_MASK = struct.Struct("=I")
_TIMESTAMP = struct.Struct("=qI")
_BTIME_OFFSET = 80


def _load_statx() -> Callable[..., int] | None:
    libc_name = ctypes.util.find_library("c")
    if libc_name is None:  # pragma: no cover - glibc/musl siempre presentes en Linux
        return None
    func = getattr(ctypes.CDLL(libc_name, use_errno=True), "statx", None)
    if func is None:  # pragma: no cover - glibc < 2.28
        return None
    func.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_uint, ctypes.c_void_p]
    func.restype = ctypes.c_int
    return func  # type: ignore[no-any-return]


_statx = _load_statx()


def birth_time_ns(path: Path) -> int | None:
    """``btime`` en nanosegundos, o ``None`` si el sistema de archivos no la registra."""
    if _statx is None:  # pragma: no cover
        return None
    buf = ctypes.create_string_buffer(_STATX_SIZE)
    if _statx(_AT_FDCWD, os.fsencode(path), _AT_SYMLINK_NOFOLLOW, _STATX_BTIME, buf) != 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err), str(path))
    (mask,) = _MASK.unpack_from(buf.raw, 0)
    if not mask & _STATX_BTIME:
        return None
    sec, nsec = _TIMESTAMP.unpack_from(buf.raw, _BTIME_OFFSET)
    return int(sec) * 1_000_000_000 + int(nsec)
