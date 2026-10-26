from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pytestqt.qtbot import QtBot

from airgnizer.app import AppContext
from airgnizer.cli import main as cli
from airgnizer.core.config import (
    LearningSettings,
    Settings,
    WatchSettings,
    save_settings,
)
from airgnizer.core.paths import AppPaths
from airgnizer.gui.app import build
from airgnizer.gui.controller import Controller
from airgnizer.gui.window import MainWindow


@pytest.fixture
def data(isolated_home: Path) -> Path:
    """Datos simulados: carpetas organizadas, un duplicado y dos descargas pendientes."""
    home = isolated_home
    downloads, docs = home / "Descargas", home / "Documentos"
    for folder in (downloads, docs / "Banco", docs / "Fotos"):
        folder.mkdir(parents=True)
    for i in range(6):
        (docs / "Banco" / f"cartola_2024-0{i + 1}.pdf").write_bytes(b"%PDF" + bytes([i]))
        (docs / "Fotos" / f"IMG_2024010{i}_120000.jpg").write_bytes(b"img" + bytes([i]))
    (docs / "Fotos" / "copia.pdf").write_bytes(b"%PDF" + bytes([0]))  # = cartola_2024-01.pdf
    save_settings(
        AppPaths.from_env(),
        Settings(
            indexed_dirs=[docs],
            watched_dirs=[downloads],
            watch=WatchSettings(stable_seconds=0.01),
            learning=LearningSettings(rules={"ext:pdf": str(docs / "Banco")}, type_destinations={}),
        ),
    )
    (downloads / "cartola_2024-07.pdf").write_bytes(b"%PDF-nueva")
    (downloads / "raro.xyz").write_bytes(b"?")
    assert cli(["watch", "--once"]) == 0
    assert cli(["scan"]) == 0
    return home


@pytest.fixture
def gui(qtbot: QtBot, data: Path, xdg_data: Path) -> Iterator[tuple[Controller, MainWindow]]:
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance()
    assert isinstance(app, QApplication)
    ctx = AppContext.open()
    ctrl, window, tray = build(app, ctx)
    qtbot.addWidget(window)
    window.show()
    yield ctrl, window
    if tray is not None:
        tray.hide()
    ctrl.close()
    ctx.db.dispose()


@pytest.fixture
def xdg_data(data: Path) -> Path:
    """Papelera dentro del HOME aislado (``~/.local/share/Trash``)."""
    return data / ".local/share"


def idle(qtbot: QtBot, ctrl: Controller) -> None:
    """Espera a que terminen las tareas en segundo plano y se entreguen sus resultados."""
    qtbot.waitUntil(lambda: not ctrl.busy, timeout=10000)
