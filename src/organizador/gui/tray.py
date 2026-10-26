"""Icono de la bandeja con el número de descargas pendientes."""

from __future__ import annotations

from PySide6.QtCore import QRect, Qt, Signal
from PySide6.QtGui import QAction, QColor, QFont, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QMenu, QSystemTrayIcon, QWidget

SIZE = 64


def badge(base: QIcon, count: int) -> QIcon:
    """``base`` con un círculo y el número (``99+`` a partir de 100); sin número si es 0."""
    pixmap = base.pixmap(SIZE, SIZE)
    if count <= 0:
        return QIcon(pixmap)
    pixmap = QPixmap(pixmap)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    diameter = SIZE * 0.6
    rect = QRect(int(SIZE - diameter), int(SIZE - diameter), int(diameter), int(diameter))
    painter.setBrush(QColor("#da4453"))  # rojo de Breeze para avisos
    painter.setPen(Qt.PenStyle.NoPen)
    painter.drawEllipse(rect)
    font = QFont()
    font.setBold(True)
    font.setPixelSize(int(diameter * (0.42 if count > 99 else 0.6)))
    painter.setFont(font)
    painter.setPen(QColor("white"))
    painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, "99+" if count > 99 else str(count))
    painter.end()
    return QIcon(pixmap)


class Tray(QSystemTrayIcon):
    open_requested = Signal()
    quit_requested = Signal()

    def __init__(self, base: QIcon, parent: QWidget | None = None) -> None:
        super().__init__(base, parent)
        self._base = base
        self.count = 0
        menu = QMenu(parent)
        show = QAction("Abrir Organizador", menu)
        show.triggered.connect(self.open_requested.emit)
        quit_ = QAction("Salir", menu)
        quit_.triggered.connect(self.quit_requested.emit)
        menu.addAction(show)
        menu.addSeparator()
        menu.addAction(quit_)
        self._menu = menu
        self.setContextMenu(menu)
        self.activated.connect(self._activated)
        self.set_count(0)

    def set_count(self, count: int) -> None:
        self.count = count
        self.setIcon(badge(self._base, count))
        self.setToolTip(
            "Organizador: ninguna descarga pendiente"
            if count == 0
            else f"Organizador: {count} descargas pendientes"
            if count > 1
            else "Organizador: 1 descarga pendiente"
        )

    def _activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            self.open_requested.emit()
