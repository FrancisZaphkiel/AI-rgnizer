"""Punto de entrada de la interfaz gráfica: ``organizador gui``."""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Sequence

from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QStyle, QSystemTrayIcon

from organizador import __version__
from organizador.app import AppContext
from organizador.backend import open_backend
from organizador.gui.controller import Controller
from organizador.gui.tray import Tray
from organizador.gui.window import MainWindow
from organizador.service.client import RemoteBackend
from organizador.service.protocol import BUS_NAME

log = logging.getLogger(__name__)


def build(app: QApplication, ctx: AppContext) -> tuple[Controller, MainWindow, Tray | None]:
    ctrl = Controller(ctx, open_backend(ctx))
    window = MainWindow(ctrl)
    tray: Tray | None = None
    if QSystemTrayIcon.isSystemTrayAvailable():
        base = QIcon.fromTheme(
            "folder-download", app.style().standardIcon(QStyle.StandardPixmap.SP_ArrowDown)
        )
        tray = Tray(base, window)
        ctrl.pending_count.connect(tray.set_count)
        tray.open_requested.connect(window.bring_to_front)
        tray.quit_requested.connect(app.quit)
        tray.show()
        window.keep_in_tray = True
    ctrl.refresh_count()
    return ctrl, window, tray


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="organizador gui")
    parser.add_argument("--hidden", action="store_true", help="arranca solo en la bandeja")
    args, qt_args = parser.parse_known_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

    app = QApplication([sys.argv[0], *qt_args])
    app.setApplicationName("Organizador")
    app.setApplicationVersion(__version__)
    # En Wayland identifica la ventana (y su icono) con el archivo .desktop de la Fase 6.
    app.setDesktopFileName(BUS_NAME)
    app.setWindowIcon(QIcon.fromTheme("folder-download"))

    ctx = AppContext.open()
    ctrl, window, tray = build(app, ctx)
    if tray is not None:
        # Cerrar la ventana la deja en la bandeja: salir es explícito.
        app.setQuitOnLastWindowClosed(False)
    if not (args.hidden and tray is not None):
        window.show()
    try:
        app.aboutToQuit.connect(ctrl.close)
        return app.exec()
    finally:
        if isinstance(ctrl.backend, RemoteBackend):
            ctrl.backend.client.close()
        ctrl.runner.pool.waitForDone(5000)
        ctx.db.dispose()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
