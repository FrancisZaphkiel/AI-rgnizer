"""Ventana principal: navegación lateral y una vista por sección."""

from __future__ import annotations

from PySide6.QtCore import QSize
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QHBoxLayout,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QStackedWidget,
    QStyle,
    QWidget,
)

from airgnizer.gui.common import icon
from airgnizer.gui.controller import Controller
from airgnizer.gui.views.downloads import DownloadsView
from airgnizer.gui.views.duplicates import DuplicatesView
from airgnizer.gui.views.history import HistoryView
from airgnizer.gui.views.panel import PanelView
from airgnizer.gui.views.patterns import PatternsView
from airgnizer.gui.views.settings import SettingsView

SP = QStyle.StandardPixmap
SECTIONS = [
    ("Panel", "view-dashboard", SP.SP_ComputerIcon),
    ("Descargas", "folder-download", SP.SP_ArrowDown),
    ("Duplicados", "edit-copy", SP.SP_FileDialogDetailedView),
    ("Patrones", "format-text-code", SP.SP_FileDialogListView),
    ("Historial", "view-history", SP.SP_BrowserReload),
    ("Ajustes", "configure", SP.SP_FileDialogInfoView),
]


class MainWindow(QMainWindow):
    def __init__(self, ctrl: Controller) -> None:
        super().__init__()
        self._ctrl = ctrl
        self.keep_in_tray = False  # lo activa la aplicación si hay bandeja
        self.setWindowTitle("AI-rgnizer")
        self.resize(1100, 700)

        self.panel = PanelView(ctrl)
        self.downloads = DownloadsView(ctrl)
        self.duplicates = DuplicatesView(ctrl)
        self.patterns = PatternsView(ctrl)
        self.history = HistoryView(ctrl)
        self.settings = SettingsView(ctrl)
        self.views: list[QWidget] = [
            self.panel,
            self.downloads,
            self.duplicates,
            self.patterns,
            self.history,
            self.settings,
        ]

        self.nav = QListWidget()
        self.nav.setIconSize(QSize(22, 22))
        self.nav.setFixedWidth(180)
        self.stack = QStackedWidget()
        for (title, theme_icon, fallback), view in zip(SECTIONS, self.views, strict=True):
            QListWidgetItem(icon(self, theme_icon, fallback), title, self.nav)
            self.stack.addWidget(view)
        self.nav.currentRowChanged.connect(self.show_section)

        central = QWidget()
        layout = QHBoxLayout(central)
        layout.addWidget(self.nav)
        layout.addWidget(self.stack, 1)
        self.setCentralWidget(central)

        self.panel.open_downloads.connect(lambda: self.nav.setCurrentRow(1))
        self.panel.open_duplicates.connect(self._open_duplicates)
        ctrl.pending_count.connect(self._show_count)
        ctrl.error.connect(self.show_error)
        self.statusBar().showMessage(
            "Conectado al servicio"
            if ctrl.remote
            else "Sin servicio: las operaciones se hacen aquí"
        )
        self.nav.setCurrentRow(0)

    def show_section(self, row: int) -> None:
        self.stack.setCurrentIndex(row)
        view = self.views[row]
        refresh = getattr(view, "refresh", None)
        if callable(refresh):
            refresh()

    def _open_duplicates(self) -> None:
        self.nav.setCurrentRow(2)
        self.duplicates.start_search()

    def _show_count(self, count: int) -> None:
        item = self.nav.item(1)
        if item is not None:
            item.setText(f"Descargas ({count})" if count else "Descargas")

    def bring_to_front(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def show_error(self, message: str) -> None:
        box = QMessageBox(QMessageBox.Icon.Warning, "AI-rgnizer", message, parent=self)
        box.open()  # no bloquea: los demás hilos siguen

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - API de Qt
        if self.keep_in_tray:
            event.ignore()
            self.hide()
        else:
            event.accept()
