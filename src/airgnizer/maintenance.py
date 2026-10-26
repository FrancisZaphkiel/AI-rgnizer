"""Exportar y borrar los datos de la aplicación, y arranque con la sesión."""

from __future__ import annotations

import shlex
import sqlite3
import subprocess
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path

from airgnizer.actions import fsops
from airgnizer.app import AppContext
from airgnizer.core.paths import config_home
from airgnizer.desktop import ICON_NAME
from airgnizer.launcher import self_command
from airgnizer.service import systemd
from airgnizer.storage import Database

AUTOSTART_NAME = "io.github.airgnizer.AIrgnizer.desktop"


class ServiceBusyError(RuntimeError):
    """El servicio está en marcha fuera de systemd: hay que detenerlo a mano."""


def export_data(ctx: AppContext, dest_dir: Path) -> Path:
    """Zip con la configuración, una copia coherente de la base y los modelos."""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = dest_dir / f"airgnizer-datos-{stamp}.zip"
    with tempfile.TemporaryDirectory() as tmp:
        snapshot = Path(tmp) / "airgnizer.db"
        # La API de copia de SQLite da una instantánea coherente aunque el servicio escriba.
        source = sqlite3.connect(f"file:{ctx.paths.database_file}?mode=ro", uri=True)
        copy = sqlite3.connect(snapshot)
        try:
            source.backup(copy)
        finally:
            copy.close()
            source.close()
        with target.open("xb") as fh, zipfile.ZipFile(fh, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.write(snapshot, "airgnizer.db")
            if ctx.paths.config_file.exists():
                zf.write(ctx.paths.config_file, "config.toml")
            models = ctx.paths.data_dir / "models"
            if models.is_dir():
                for path in sorted(models.iterdir()):
                    if path.is_file():
                        zf.write(path, f"models/{path.name}")
    return target


def service_active() -> bool:
    return (
        subprocess.call(
            ["systemctl", "--user", "--quiet", "is-active", systemd.UNIT_NAME],
            stderr=subprocess.DEVNULL,
        )
        == 0
    )


def erase_learned_data(ctx: AppContext, *, service_running: bool) -> list[Path]:
    """Borra la base de datos (índice, historial, aprendizaje) y los modelos.

    Detiene el servicio de systemd mientras tanto y lo vuelve a arrancar con una base
    vacía. Los archivos del usuario y la configuración no se tocan. Si el servicio está
    en marcha fuera de systemd lanza ``ServiceBusyError``.
    """
    under_systemd = service_active()
    if service_running and not under_systemd:
        raise ServiceBusyError("Detén el servicio («airgnizer daemon») antes de borrar los datos")
    if under_systemd:
        systemd.systemctl("stop", systemd.UNIT_NAME)
    try:
        ctx.db.dispose()
        removed = fsops.remove_app_data(ctx.paths.data_dir)
        ctx.db = Database(ctx.paths.database_file)
        ctx.db.migrate()
    finally:
        if under_systemd:
            systemd.systemctl("start", systemd.UNIT_NAME)
    return removed


# --- arranque con la sesión ----------------------------------------------------------------


def service_enabled() -> bool:
    return (
        subprocess.call(
            ["systemctl", "--user", "--quiet", "is-enabled", systemd.UNIT_NAME],
            stderr=subprocess.DEVNULL,
        )
        == 0
    )


def set_service_autostart(enabled: bool) -> int:
    """Instala y habilita (o deshabilita) el servicio de systemd --user."""
    if enabled:
        systemd.install()
        code = systemd.systemctl("daemon-reload")
        return code or systemd.systemctl("enable", "--now", systemd.UNIT_NAME)
    return systemd.systemctl("disable", "--now", systemd.UNIT_NAME)


def autostart_entry() -> Path:
    return config_home() / "autostart" / AUTOSTART_NAME


def tray_autostart_enabled() -> bool:
    return autostart_entry().is_file()


def set_tray_autostart(enabled: bool) -> None:
    """Muestra el icono de la bandeja al iniciar sesión (``~/.config/autostart``)."""
    path = autostart_entry()
    if not enabled:
        fsops.remove_autostart_entry(path)
        return
    command = shlex.join(self_command("gui", "--hidden"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=AI-rgnizer\n"
        "Comment=Icono de la bandeja con las descargas pendientes\n"
        f"Exec={command}\n"
        f"Icon={ICON_NAME}\n"
        "X-GNOME-Autostart-enabled=true\n",
        encoding="utf-8",
    )
