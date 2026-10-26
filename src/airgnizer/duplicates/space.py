"""Espacio en disco que ocupa realmente un archivo.

Dos copias idénticas no siempre ocupan el doble: los hardlinks comparten inode y los
reflinks de Btrfs/XFS (``cp --reflink``) comparten extents. Para no prometer espacio
que no se libera, se consulta ``FS_IOC_FIEMAP`` y se descuentan los extents marcados
como compartidos. La estimación es conservadora: un extent compartido solo entre
copias que se eliminan todas también se descuenta.
"""

from __future__ import annotations

import errno
import fcntl
import logging
import os
import struct
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

# <linux/fs.h>: _IOWR('f', 11, struct fiemap)
FS_IOC_FIEMAP = 0xC020660B
FIEMAP_FLAG_SYNC = 0x1
FIEMAP_EXTENT_LAST = 0x1
FIEMAP_EXTENT_SHARED = 0x2000
FIEMAP_MAX_OFFSET = 2**64 - 1

# struct fiemap: fm_start, fm_length, fm_flags, fm_mapped_extents, fm_extent_count, fm_reserved
_HEADER = struct.Struct("=QQIIII")
# struct fiemap_extent: fe_logical, fe_physical, fe_length, 2×reserved64, fe_flags, 3×reserved
_EXTENT = struct.Struct("=QQQQQIIII")
_BATCH = 256


@dataclass(frozen=True, slots=True)
class DiskUsage:
    allocated: int
    # Bytes en extents compartidos con otros archivos (reflinks, instantáneas).
    shared: int

    @property
    def exclusive(self) -> int:
        return max(0, self.allocated - self.shared)


def _fiemap(fd: int) -> DiskUsage | None:
    allocated = shared = 0
    start = 0
    while True:
        request = bytearray(_HEADER.size + _EXTENT.size * _BATCH)
        _HEADER.pack_into(
            request, 0, start, FIEMAP_MAX_OFFSET - start, FIEMAP_FLAG_SYNC, 0, _BATCH, 0
        )
        try:
            fcntl.ioctl(fd, FS_IOC_FIEMAP, request)
        except OSError as exc:
            if exc.errno in (errno.EOPNOTSUPP, errno.ENOTTY, errno.EINVAL):
                return None
            raise
        mapped = _HEADER.unpack_from(request, 0)[3]
        if mapped == 0:
            return DiskUsage(allocated, shared)
        for i in range(mapped):
            logical, _phys, length, _r1, _r2, flags, *_ = _EXTENT.unpack_from(
                request, _HEADER.size + i * _EXTENT.size
            )
            allocated += length
            if flags & FIEMAP_EXTENT_SHARED:
                shared += length
            if flags & FIEMAP_EXTENT_LAST:
                return DiskUsage(allocated, shared)
            start = logical + length


def disk_usage(path: Path) -> DiskUsage:
    """Uso real en disco; sin FIEMAP (p. ej. tmpfs) se usa ``st_blocks`` sin compartidos."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        usage = _fiemap(fd)
        if usage is None:
            log.debug("FIEMAP no disponible para %s; se usa st_blocks", path)
            usage = DiskUsage(os.fstat(fd).st_blocks * 512, 0)
        return usage
    finally:
        os.close(fd)
