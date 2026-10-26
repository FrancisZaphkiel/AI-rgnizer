"""Entrada del menú de aplicaciones e icono (``~/.local/share/applications``).

Con ella AI-rgnizer aparece en el menú de KDE, la ventana muestra su icono en Wayland y
el portal de escritorio reconoce el identificador de la aplicación (sin ella avisa
«App info not found»). Al ejecutar la AppImage se instala o actualiza sola; desde el
código fuente, con ``airgnizer desktop install``.
"""

from __future__ import annotations

import shlex
import shutil
from importlib.resources import files
from pathlib import Path

from airgnizer.actions import fsops
from airgnizer.core.paths import data_home
from airgnizer.launcher import self_command
from airgnizer.service.protocol import BUS_NAME

APP_ID = BUS_NAME
ICON_NAME = APP_ID


def menu_entry() -> Path:
    return data_home() / "applications" / f"{APP_ID}.desktop"


def icon_file() -> Path:
    return data_home() / "icons" / "hicolor" / "scalable" / "apps" / f"{ICON_NAME}.svg"


def icon_source() -> Path:
    return Path(str(files("airgnizer") / "data" / f"{ICON_NAME}.svg"))


def entry_text(command: list[str] | None = None) -> str:
    exec_line = shlex.join(command or self_command("gui"))
    return (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=AI-rgnizer\n"
        "GenericName=Organizador de archivos\n"
        "Comment=Aprende dónde guardas tus archivos, ordena las descargas y busca duplicados\n"
        f"Exec={exec_line}\n"
        f"Icon={ICON_NAME}\n"
        "Terminal=false\n"
        "Categories=Utility;FileTools;Qt;\n"
        "Keywords=archivos;descargas;duplicados;ordenar;organizar;files;downloads;\n"
        "StartupNotify=true\n"
        f"StartupWMClass={APP_ID}\n"
    )


def install(command: list[str] | None = None) -> list[Path]:
    """Escribe (o actualiza) la entrada del menú y el icono."""
    entry, icon = menu_entry(), icon_file()
    entry.parent.mkdir(parents=True, exist_ok=True)
    icon.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(icon_source(), icon)
    tmp = entry.with_suffix(".desktop.tmp")
    tmp.write_text(entry_text(command), encoding="utf-8")
    tmp.replace(entry)
    return [entry, icon]


def up_to_date(command: list[str] | None = None) -> bool:
    """¿Está instalada y apunta a este mismo programa (p. ej. la AppImage se movió)?"""
    try:
        return menu_entry().read_text(encoding="utf-8") == entry_text(command) and (
            icon_file().is_file()
        )
    except OSError:
        return False


def uninstall() -> list[Path]:
    removed = [p for p in (menu_entry(), icon_file()) if p.exists()]
    for path in removed:
        fsops.remove_menu_entry(path)
    return removed
