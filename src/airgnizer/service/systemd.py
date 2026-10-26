"""Unidad ``systemd --user`` y activación D-Bus del servicio.

- ``Type=dbus``: systemd da el servicio por arrancado cuando toma su nombre en el bus.
- ``Restart=on-failure``: si el proceso falla, systemd lo reinicia; la cola deduplica
  por huella, así que el reinicio no repite sugerencias.
- El archivo de activación D-Bus hace que una llamada al nombre (desde la GUI) arranque
  la unidad si no estaba en marcha.
"""

from __future__ import annotations

import shlex
import subprocess
from pathlib import Path

from airgnizer.core.paths import config_home, data_home
from airgnizer.launcher import self_command
from airgnizer.service.protocol import BUS_NAME

UNIT_NAME = "airgnizer.service"


def unit_path() -> Path:
    return config_home() / "systemd" / "user" / UNIT_NAME


def activation_path() -> Path:
    return data_home() / "dbus-1" / "services" / f"{BUS_NAME}.service"


def daemon_command() -> list[str]:
    """Este mismo programa: el .venv de desarrollo o el archivo .AppImage."""
    return self_command("daemon")


def unit_text(command: list[str]) -> str:
    return f"""\
[Unit]
Description=AI-rgnizer: vigila las descargas y sugiere dónde guardarlas
PartOf=graphical-session.target
After=graphical-session.target

[Service]
Type=dbus
BusName={BUS_NAME}
ExecStart={shlex.join(command)}
Restart=on-failure
RestartSec=5
# Arranca leyendo el índice: con muchas carpetas, los 15 s por defecto pueden no bastar.
TimeoutStartSec=90
Environment=PYTHONUNBUFFERED=1
# El modelo es pequeño: un hilo de BLAS basta y evita decenas de hilos ociosos.
Environment=OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1

[Install]
WantedBy=graphical-session.target
"""


def activation_text() -> str:
    return f"""\
[D-BUS Service]
Name={BUS_NAME}
Exec=/bin/false
SystemdService={UNIT_NAME}
"""


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def install(command: list[str] | None = None) -> list[Path]:
    """Escribe la unidad y la activación D-Bus; no arranca nada."""
    _write(unit_path(), unit_text(command or daemon_command()))
    _write(activation_path(), activation_text())
    return [unit_path(), activation_path()]


def refresh_if_moved() -> bool:
    """Si la unidad instalada llama a otro programa (la AppImage se movió), la reescribe.

    Devuelve ``True`` si la actualizó; no instala nada que no estuviera instalado.
    """
    try:
        current = unit_path().read_text(encoding="utf-8")
    except OSError:
        return False
    if current == unit_text(daemon_command()):
        return False
    install()
    systemctl("daemon-reload")
    systemctl("try-restart", UNIT_NAME)  # solo si estaba en marcha (o intentándolo)
    return True


def installed_files() -> list[Path]:
    return [p for p in (unit_path(), activation_path()) if p.exists()]


def systemctl(*args: str) -> int:
    return subprocess.call(["systemctl", "--user", *args])
