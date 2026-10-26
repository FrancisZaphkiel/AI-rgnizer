"""Actualización desde «organizador» (≤ 0.3.0): servicio, inicio de sesión y datos.

Se ejecuta al arrancar la CLI o la GUI y no hace nada si no queda rastro del nombre
antiguo. Orden: primero se detiene y retira el servicio antiguo (tiene la base abierta),
luego se trasladan las carpetas de datos y, por último, se instala con el nombre nuevo
lo que estuviera activado (servicio al iniciar sesión, icono en la bandeja).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from airgnizer.actions import fsops
from airgnizer.core import legacy
from airgnizer.core.paths import AppPaths, config_home, data_home
from airgnizer.service import systemd

log = logging.getLogger(__name__)


@dataclass(slots=True)
class UpgradeReport:
    moved: list[legacy.Moved] = field(default_factory=list)
    service_restored: bool = False
    tray_restored: bool = False

    def __bool__(self) -> bool:
        return bool(self.moved or self.service_restored or self.tray_restored)


def _retire_legacy_service() -> bool:
    """Detiene y quita ``organizador.service``; ``True`` si estaba activado."""
    unit = config_home() / "systemd" / "user" / legacy.LEGACY_UNIT
    activation = data_home() / "dbus-1" / "services" / f"{legacy.LEGACY_BUS_NAME}.service"
    if not unit.exists() and not activation.exists():
        return False
    was_enabled = systemd.systemctl("--quiet", "is-enabled", legacy.LEGACY_UNIT) == 0
    systemd.systemctl("disable", "--now", legacy.LEGACY_UNIT)
    for path in (unit, activation):
        fsops.remove_service_file(path)
    systemd.systemctl("daemon-reload")
    log.info("Retirado el servicio antiguo %s", legacy.LEGACY_UNIT)
    return was_enabled


def _retire_legacy_autostart() -> bool:
    entry = config_home() / "autostart" / legacy.LEGACY_AUTOSTART
    if not entry.exists():
        return False
    fsops.remove_autostart_entry(entry)
    return True


def upgrade(paths: AppPaths) -> UpgradeReport:
    """Lanza ``LegacyServiceRunningError`` si el servicio antiguo no se pudo detener."""
    from airgnizer import maintenance  # noqa: PLC0415 - importa la app entera; solo si hace falta

    report = UpgradeReport()
    service = _retire_legacy_service()
    tray = _retire_legacy_autostart()
    report.moved = legacy.migrate(paths)
    if service:
        report.service_restored = maintenance.set_service_autostart(True) == 0
    if tray:
        maintenance.set_tray_autostart(True)
        report.tray_restored = True
    return report
