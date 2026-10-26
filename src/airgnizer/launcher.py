"""Cómo volver a lanzar este mismo programa (servicio, inicio de sesión, menú).

Dentro de una AppImage, ``sys.executable`` vive en un montaje temporal que desaparece al
cerrar el programa: la unidad de systemd o la entrada del menú deben llamar al archivo
``.AppImage`` (la variable ``APPIMAGE`` que pone su runtime). Fuera de ella, el
intérprete actual con ``-m airgnizer`` sirve igual para el entorno de desarrollo.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def appimage() -> str | None:
    """Ruta del archivo .AppImage si se ejecuta desde una; ``None`` si no."""
    path = os.environ.get("APPIMAGE")
    return path if path and Path(path).is_file() else None


def self_command(*args: str) -> list[str]:
    if (image := appimage()) is not None:
        return [image, *args]
    return [sys.executable, "-m", "airgnizer", *args]
