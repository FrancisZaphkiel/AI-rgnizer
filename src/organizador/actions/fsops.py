"""Operaciones de bajo nivel sobre el sistema de archivos.

Este es el **único** módulo autorizado a eliminar entradas del sistema de
archivos (ruff ``TID251`` lo prohíbe en el resto y un test lo audita). Cada
función que borra comprueba antes que lo que elimina es exactamente lo esperado:

- ``copy_verify_unlink``: borra el origen solo tras verificar la copia por hash.
- ``complete_interrupted_copy``: idem, para recuperar una copia interrumpida.
- ``discard_temp``: borra únicamente temporales creados por esta aplicación.
- ``remove_trashinfo``: borra únicamente archivos ``.trashinfo`` de una papelera.
- ``discard_model_version``: borra únicamente versiones antiguas del modelo de la aplicación.

Ninguna función sobrescribe jamás un archivo existente.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import errno
import os
import re
import shutil
from collections.abc import Callable
from pathlib import Path

import blake3

TEMP_MARKER = ".organizador-"
TEMP_SUFFIX = ".tmp"
_CHUNK = 1024 * 1024

_AT_FDCWD = -100
_RENAME_NOREPLACE = 1


class CrossDeviceError(OSError):
    """El origen y el destino están en sistemas de archivos (o subvolúmenes) distintos."""


class VerificationError(OSError):
    """La copia no coincide con el original; no se ha borrado nada."""


def _load_renameat2() -> Callable[..., int] | None:
    libc_name = ctypes.util.find_library("c")
    if libc_name is None:  # pragma: no cover - glibc/musl siempre presentes en Linux
        return None
    libc = ctypes.CDLL(libc_name, use_errno=True)
    func = getattr(libc, "renameat2", None)
    if func is None:  # pragma: no cover - glibc < 2.28
        return None
    func.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    func.restype = ctypes.c_int
    return func  # type: ignore[no-any-return]


_renameat2 = _load_renameat2()


def hash_file(path: Path) -> str:
    """BLAKE3 del contenido completo (hex, 64 caracteres)."""
    hasher = blake3.blake3()
    with path.open("rb") as fh:
        while chunk := fh.read(_CHUNK):
            hasher.update(chunk)
    return str(hasher.hexdigest())


def fsync_dir(directory: Path) -> None:
    """Persiste en disco las entradas (creaciones/renombrados) de un directorio."""
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _link_then_unlink(src: Path, dst: Path) -> None:
    # Alternativa atómica sin sobrescritura para sistemas sin RENAME_NOREPLACE:
    # link() falla con EEXIST si el destino existe.
    os.link(src, dst, follow_symlinks=False)
    os.unlink(src)


def move_no_replace(src: Path, dst: Path) -> None:
    """Renombra ``src`` a ``dst`` de forma atómica sin sobrescribir nunca.

    Raises:
        FileExistsError: ``dst`` ya existe.
        CrossDeviceError: distinto sistema de archivos o subvolumen Btrfs.
    """
    if _renameat2 is not None:
        result = _renameat2(
            _AT_FDCWD, os.fsencode(src), _AT_FDCWD, os.fsencode(dst), _RENAME_NOREPLACE
        )
        if result == 0:
            return
        err = ctypes.get_errno()
        if err not in (errno.EINVAL, errno.ENOSYS):
            if err == errno.EXDEV:
                raise CrossDeviceError(err, os.strerror(err), str(src), None, str(dst))
            raise OSError(err, os.strerror(err), str(src), None, str(dst))
        # EINVAL/ENOSYS: el sistema de archivos no soporta el flag (p. ej. algunos FUSE).
    try:
        _link_then_unlink(src, dst)
    except OSError as exc:
        if exc.errno == errno.EXDEV:
            raise CrossDeviceError(*exc.args) from exc
        raise


def temp_path_for(dst: Path, token: str) -> Path:
    """Ruta temporal oculta, junto al destino, identificable como propia."""
    return dst.with_name(f".{dst.name}{TEMP_MARKER}{token}{TEMP_SUFFIX}")


def is_own_temp(path: Path) -> bool:
    return path.name.startswith(".") and TEMP_MARKER in path.name and path.suffix == TEMP_SUFFIX


def _copy_exclusive(src: Path, temp: Path) -> None:
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with src.open("rb") as fin, os.fdopen(fd, "wb") as fout:
        shutil.copyfileobj(fin, fout, _CHUNK)
        fout.flush()
        os.fsync(fout.fileno())
    shutil.copystat(src, temp)


def copy_verify_unlink(src: Path, dst: Path, temp: Path, expected_hash: str) -> None:
    """Mueve entre sistemas de archivos: copia → verifica → publica → borra el origen.

    En cualquier punto de fallo el origen sigue intacto; como mucho queda ``temp``,
    que ``discard_temp`` puede limpiar durante la recuperación.
    """
    if hash_file(src) != expected_hash:
        raise VerificationError(errno.EIO, "El origen cambió antes de copiarlo", str(src))
    _copy_exclusive(src, temp)
    if hash_file(temp) != expected_hash:
        discard_temp(temp)
        raise VerificationError(errno.EIO, "La copia no coincide con el original", str(src))
    move_no_replace(temp, dst)
    fsync_dir(dst.parent)
    os.unlink(src)
    fsync_dir(src.parent)


def complete_interrupted_copy(src: Path, dst: Path) -> bool:
    """Termina un movimiento entre sistemas de archivos interrumpido tras publicar ``dst``.

    Borra ``src`` solo si su contenido es idéntico a ``dst``. Devuelve ``True`` si lo hizo.
    """
    if src.stat().st_size != dst.stat().st_size or hash_file(src) != hash_file(dst):
        return False
    os.unlink(src)
    fsync_dir(src.parent)
    return True


def discard_temp(temp: Path) -> None:
    """Borra un temporal creado por ``copy_verify_unlink``; rechaza cualquier otra ruta."""
    if not is_own_temp(temp):
        raise ValueError(f"No es un temporal de la aplicación: {temp}")
    try:
        os.unlink(temp)
    except FileNotFoundError:
        return


def remove_trashinfo(info_path: Path) -> None:
    """Borra un ``.trashinfo`` (tras restaurar un archivo); rechaza cualquier otra ruta."""
    if info_path.suffix != ".trashinfo" or info_path.parent.name != "info":
        raise ValueError(f"No es un archivo .trashinfo de una papelera: {info_path}")
    try:
        os.unlink(info_path)
    except FileNotFoundError:
        return


MODEL_FILE = re.compile(r"model-\d{4,}\.joblib")


def discard_model_version(path: Path, models_dir: Path) -> None:
    """Borra una versión antigua del modelo; rechaza cualquier ruta fuera de ``models_dir``.

    Los modelos son datos de la aplicación (se regeneran entrenando), nunca del usuario.
    """
    if path.parent != models_dir or not MODEL_FILE.fullmatch(path.name):
        raise ValueError(f"No es una versión del modelo de la aplicación: {path}")
    try:
        os.unlink(path)
    except FileNotFoundError:
        return
