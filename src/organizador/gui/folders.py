"""Elegir carpetas, con las unidades montadas a mano (discos externos, USB, red…).

El diálogo de carpetas del portal no siempre muestra los dispositivos en el panel lateral.
Por eso cada botón «Añadir…» o «Mover a…» abre un menú con «Elegir carpeta…» y una
entrada por unidad montada; al pulsar una unidad, el diálogo se abre dentro de ella.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import QUrl, Signal
from PySide6.QtGui import QAction, QIcon
from PySide6.QtWidgets import (
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QMenu,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from organizador.gui.common import human_size, pretty
from organizador.scanner.mounts import MountEntry, user_mounts


def drive_label(entry: MountEntry) -> str:
    """«unidad2nvme — btrfs, 120.5 GiB libres»."""
    try:
        free = f", {human_size(shutil.disk_usage(entry.point).free)} libres"
    except OSError:
        free = ""
    return f"{entry.point.name or entry.point} — {entry.fstype}{free}"


def choose_directory(parent: QWidget, title: str, start: Path | str) -> Path | None:
    """Selector de carpetas del sistema; las unidades montadas, en el panel lateral."""
    dialog = QFileDialog(parent, title, str(start))
    dialog.setFileMode(QFileDialog.FileMode.Directory)
    dialog.setOption(QFileDialog.Option.ShowDirsOnly, True)
    places = [QUrl.fromLocalFile(str(Path.home()))]
    places += [QUrl.fromLocalFile(str(e.point)) for e in user_mounts()]
    dialog.setSidebarUrls(places)  # el diálogo de Qt; el de KDE ya muestra «Dispositivos»
    if not dialog.exec():
        return None
    chosen = dialog.selectedFiles()
    return Path(chosen[0]) if chosen else None


def folder_menu(
    parent: QWidget,
    title: str,
    start: Callable[[], Path | str],
    on_chosen: Callable[[Path], object],
    choose: Callable[[QWidget, str, Path | str], Path | None] = choose_directory,
) -> QMenu:
    """Menú «Elegir carpeta…» + una entrada por unidad montada (se rehace al abrirlo)."""
    menu = QMenu(parent)

    def pick(where: Path | str) -> None:
        if (folder := choose(parent, title, where)) is not None:
            on_chosen(folder)

    def rebuild() -> None:
        menu.clear()
        menu.addAction(QIcon.fromTheme("folder-open"), "Elegir carpeta…", lambda: pick(start()))
        drives = user_mounts()
        if drives:
            menu.addSection("Unidades montadas")
        for entry in drives:
            action = QAction(QIcon.fromTheme("drive-harddisk"), drive_label(entry), menu)
            action.setToolTip(f"Elegir una carpeta dentro de {entry.point}")
            action.triggered.connect(lambda _checked=False, p=entry.point: pick(p))
            menu.addAction(action)
        if not drives:
            empty = menu.addAction("No hay unidades externas montadas")
            empty.setEnabled(False)

    menu.aboutToShow.connect(rebuild)
    rebuild()
    return menu


class FolderList(QGroupBox):
    """Lista de carpetas con «Añadir…» (incluidas las unidades montadas) y «Quitar»."""

    changed = Signal()

    def __init__(self, title: str, hint: str, empty_text: str = "") -> None:
        super().__init__(title)
        layout = QHBoxLayout(self)
        column = QVBoxLayout()
        self.list = QListWidget()
        self.list.setToolTip(hint)
        column.addWidget(self.list)
        self.empty = QLabel(empty_text)
        self.empty.setWordWrap(True)
        self.empty.setEnabled(False)  # tono secundario del tema
        column.addWidget(self.empty)
        layout.addLayout(column, 1)
        buttons = QVBoxLayout()
        self.add = QPushButton("Añadir…")
        self.add.setMenu(folder_menu(self, title, Path.home, lambda p: self.add_path(str(p))))
        remove = QPushButton("Quitar")
        remove.clicked.connect(self._remove)
        buttons.addWidget(self.add)
        buttons.addWidget(remove)
        buttons.addStretch()
        layout.addLayout(buttons)
        self._show_empty()

    def set_paths(self, paths: list[Path] | list[str]) -> None:
        self.list.clear()
        for p in paths:
            self.list.addItem(str(p))
            item = self.list.item(self.list.count() - 1)
            item.setToolTip(pretty(p))
        self._show_empty()

    def paths(self) -> list[str]:
        return [self.list.item(i).text() for i in range(self.list.count())]

    def add_path(self, path: str) -> None:
        if path and path not in self.paths():
            self.list.addItem(path)
            self._show_empty()
            self.changed.emit()

    def _remove(self) -> None:
        items = self.list.selectedItems()
        for item in items:
            self.list.takeItem(self.list.row(item))
        if items:
            self._show_empty()
            self.changed.emit()

    def _show_empty(self) -> None:
        self.empty.setVisible(bool(self.empty.text()) and self.list.count() == 0)
