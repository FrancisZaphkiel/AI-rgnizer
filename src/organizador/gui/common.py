"""Piezas comunes de las vistas."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHeaderView,
    QLabel,
    QStyle,
    QTableWidget,
    QTableWidgetItem,
    QWidget,
)

SOURCE_TEXT = {
    "configuración": "tu regla",
    "regla": "aprendido",
    "modelo": "modelo",
    "carpeta": "carpeta con ese nombre",
    "tipo": "por tipo de archivo",
}
# Fuentes sin probabilidad calibrada: se muestra «—» en vez de un porcentaje.
UNCALIBRATED = {"configuración", "carpeta", "tipo"}


def pretty(path: str | Path) -> str:
    """Ruta abreviada con ``~`` para el directorio personal."""
    text = str(path)
    home = str(Path.home())
    return "~" + text[len(home) :] if text == home or text.startswith(home + "/") else text


def human_size(n: int) -> str:
    size = float(n)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if size < 1024 or unit == "GiB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TiB"  # pragma: no cover


def icon(widget: QWidget, theme_name: str, fallback: QStyle.StandardPixmap) -> QIcon:
    """Icono del tema del sistema (Breeze en KDE) o, si no existe, uno estándar de Qt."""
    return QIcon.fromTheme(theme_name, widget.style().standardIcon(fallback))


def table(headers: list[str], *, stretch: int = 0) -> QTableWidget:
    t = QTableWidget(0, len(headers))
    t.setHorizontalHeaderLabels(headers)
    t.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    t.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    t.setAlternatingRowColors(True)
    t.verticalHeader().setVisible(False)
    header = t.horizontalHeader()
    header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
    header.setSectionResizeMode(stretch, QHeaderView.ResizeMode.Stretch)
    return t


def cell(text: str, tooltip: str | None = None) -> QTableWidgetItem:
    item = QTableWidgetItem(text)
    item.setToolTip(tooltip or text)
    return item


def heading(text: str) -> QLabel:
    label = QLabel(text)
    font = label.font()
    font.setPointSizeF(font.pointSizeF() * 1.4)
    font.setBold(True)
    label.setFont(font)
    return label
