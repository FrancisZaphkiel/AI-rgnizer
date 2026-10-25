"""Papelera según la especificación freedesktop.org Trash 1.0.

Implementación propia (en lugar de ``send2trash``) porque necesitamos:

- saber exactamente dónde quedó el archivo, para poder deshacer;
- no sobrescribir nunca (``renameat2(RENAME_NOREPLACE)``);
- no copiar entre dispositivos: en Btrfs, mover entre subvolúmenes da ``EXDEV``;
  en ese caso se usa la papelera de la raíz del subvolumen/volumen, como indica la
  especificación para ``$topdir``, y si no es posible se rechaza la operación.
"""

from __future__ import annotations

import errno
import os
import stat
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from organizador.actions import fsops
from organizador.core.paths import data_home

INFO_SUFFIX = ".trashinfo"
_MAX_NAME_ATTEMPTS = 10_000


@dataclass(frozen=True, slots=True)
class TrashDir:
    path: Path
    # Directorio base para rutas relativas en ``Path=``; ``None`` en la papelera del home.
    topdir: Path | None

    @property
    def files_dir(self) -> Path:
        return self.path / "files"

    @property
    def info_dir(self) -> Path:
        return self.path / "info"


@dataclass(frozen=True, slots=True)
class TrashedItem:
    original: Path
    files_path: Path
    info_path: Path


class TrashError(OSError):
    """No se encontró una papelera válida en el mismo sistema de archivos."""


def home_trash() -> TrashDir:
    return TrashDir(data_home() / "Trash", None)


def _device_root(path: Path) -> Path:
    """Directorio más alto que comparte ``st_dev`` con ``path`` (raíz de volumen o subvolumen)."""
    dev = path.lstat().st_dev
    current = path if path.is_dir() and not path.is_symlink() else path.parent
    while current.parent != current and current.parent.lstat().st_dev == dev:
        current = current.parent
    return current


def _valid_shared_trash(topdir: Path) -> Path | None:
    # $topdir/.Trash debe existir, no ser symlink y tener el bit sticky.
    shared = topdir / ".Trash"
    try:
        st = shared.lstat()
    except OSError:
        return None
    if stat.S_ISDIR(st.st_mode) and st.st_mode & stat.S_ISVTX:
        return shared / str(os.getuid())
    return None


def candidate_trash_dirs(path: Path) -> list[TrashDir]:
    """Papeleras a probar, en orden: la del home y luego las de ``$topdir``."""
    candidates = [home_trash()]
    topdir = _device_root(path)
    shared = _valid_shared_trash(topdir)
    if shared is not None:
        candidates.append(TrashDir(shared, topdir))
    candidates.append(TrashDir(topdir / f".Trash-{os.getuid()}", topdir))
    return candidates


def _info_content(original: Path, trash: TrashDir) -> str:
    location = original if trash.topdir is None else original.relative_to(trash.topdir)
    deleted = datetime.now().replace(microsecond=0).isoformat()
    return f"[Trash Info]\nPath={quote(str(location), safe='/')}\nDeletionDate={deleted}\n"


def _unique_names(name: str) -> list[str]:
    base, dot, ext = name.rpartition(".")
    if not base:  # sin extensión (o archivo oculto sin extensión)
        base, dot, ext = name, "", ""
    names = [name]
    names.extend(f"{base} {i}{dot}{ext}" for i in range(2, _MAX_NAME_ATTEMPTS))
    return names


def _try_trash_into(path: Path, trash: TrashDir) -> TrashedItem:
    trash.files_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    trash.info_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    content = _info_content(path, trash).encode("utf-8")

    for name in _unique_names(path.name):
        info_path = trash.info_dir / f"{name}{INFO_SUFFIX}"
        files_path = trash.files_dir / name
        # La especificación reserva el nombre creando el .trashinfo con O_EXCL.
        try:
            fd = os.open(info_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            continue
        with os.fdopen(fd, "wb") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        try:
            fsops.move_no_replace(path, files_path)
        except FileExistsError:
            fsops.remove_trashinfo(info_path)
            continue
        except OSError:
            fsops.remove_trashinfo(info_path)
            raise
        fsops.fsync_dir(trash.files_dir)
        fsops.fsync_dir(path.parent)
        return TrashedItem(path, files_path, info_path)
    raise TrashError(errno.EEXIST, "No se encontró un nombre libre en la papelera", str(path))


def move_to_trash(path: Path) -> TrashedItem:
    """Envía ``path`` a la papelera sin copiar entre dispositivos."""
    path = path.absolute()
    last_error: OSError | None = None
    for trash in candidate_trash_dirs(path):
        try:
            return _try_trash_into(path, trash)
        except fsops.CrossDeviceError as exc:
            last_error = exc
        except PermissionError as exc:
            # p. ej. no se puede crear $topdir/.Trash-$uid en un volumen ajeno.
            last_error = exc
    raise TrashError(
        errno.EXDEV,
        f"No hay una papelera utilizable en el mismo sistema de archivos ({last_error})",
        str(path),
    )


def restore(item: TrashedItem, target: Path | None = None) -> Path:
    """Devuelve el archivo a ``target`` (por defecto, su ruta original) sin sobrescribir."""
    destination = target or item.original
    destination.parent.mkdir(parents=True, exist_ok=True)
    fsops.move_no_replace(item.files_path, destination)
    fsops.fsync_dir(destination.parent)
    fsops.remove_trashinfo(item.info_path)
    return destination


def info_path_for(files_path: Path) -> Path:
    return files_path.parent.parent / "info" / f"{files_path.name}{INFO_SUFFIX}"


def locate(original: Path, inode: int, device: int) -> TrashedItem | None:
    """Busca en las papeleras candidatas un archivo por (dispositivo, inode).

    Sirve para recuperar una operación interrumpida entre el ``rename`` a la papelera
    y la actualización del journal: el rename conserva el inode.
    """
    for trash in candidate_trash_dirs(original.parent):
        try:
            entries = list(os.scandir(trash.files_dir))
        except OSError:
            continue
        for entry in entries:
            try:
                st = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            if st.st_ino == inode and st.st_dev == device:
                files_path = Path(entry.path)
                return TrashedItem(original, files_path, info_path_for(files_path))
    return None
