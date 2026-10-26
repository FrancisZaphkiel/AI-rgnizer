"""Descargas: sugerencias pendientes; aceptar, cambiar destino o ignorar, en lote."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import QItemSelectionModel, Qt
from PySide6.QtGui import QShowEvent
from PySide6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QStyle,
    QVBoxLayout,
    QWidget,
)

from organizador import overview
from organizador.downloads.queue import alternatives_of, available
from organizador.gui.common import (
    SOURCE_TEXT,
    UNCALIBRATED,
    cell,
    filling,
    heading,
    icon,
    open_path,
    pretty,
    table,
)
from organizador.gui.controller import Controller
from organizador.storage.models import DownloadSuggestion


def explanation(row: DownloadSuggestion) -> str:
    if row.destination is None:
        return "sin destino: elige una carpeta y lo aprenderé"
    text = SOURCE_TEXT.get(row.source or "", row.source or "")
    others = [Path(label).name for label, _ in alternatives_of(row) if label != row.destination]
    if others:
        text += " · otras: " + ", ".join(others[:2])
    return text


class DownloadsView(QWidget):
    def __init__(self, ctrl: Controller) -> None:
        super().__init__()
        self._ctrl = ctrl
        self._rows: list[DownloadSuggestion] = []
        layout = QVBoxLayout(self)
        layout.addWidget(heading("Descargas"))
        self.table = table(["Archivo", "Destino sugerido", "Confianza", "Motivo"], stretch=1)
        self.table.itemSelectionChanged.connect(self._update_buttons)
        self.table.doubleClicked.connect(lambda _i: self.open_selected())
        layout.addWidget(self.table)
        self.empty = QLabel("No hay descargas pendientes.")
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.empty)
        buttons = QHBoxLayout()
        self.accept = QPushButton(
            icon(self, "dialog-ok-apply", QStyle.StandardPixmap.SP_DialogApplyButton), "Aceptar"
        )
        self.accept.setToolTip("Mueve a la carpeta sugerida y lo aprende")
        self.move_to = QPushButton(
            icon(self, "folder-open", QStyle.StandardPixmap.SP_DirOpenIcon), "Mover a…"
        )
        self.move_to.setToolTip("Elige otra carpeta; el modelo aprende tu elección")
        self.ignore = QPushButton(
            icon(self, "edit-clear", QStyle.StandardPixmap.SP_DialogCancelButton), "Ignorar"
        )
        self.ignore.setToolTip("Deja el archivo donde está")
        self.open = QPushButton(
            icon(self, "document-open", QStyle.StandardPixmap.SP_FileIcon), "Abrir"
        )
        self.open.setToolTip(
            "Abre el archivo con su aplicación predeterminada para comprobar la sugerencia "
            "(también con doble clic)"
        )
        self.open.clicked.connect(self.open_selected)
        self.accept.clicked.connect(self.accept_selected)
        self.move_to.clicked.connect(self.move_selected)
        self.ignore.clicked.connect(self.ignore_selected)
        buttons.addWidget(self.open)
        buttons.addStretch()
        for b in (self.accept, self.move_to, self.ignore):
            buttons.addWidget(b)
        layout.addLayout(buttons)
        self._refresher = ctrl.refresher(
            self, lambda: overview.pending_suggestions(ctrl.ctx), self.show_rows
        )
        ctrl.changed.connect(self.refresh)
        self._update_buttons()

    def refresh(self) -> None:
        self._refresher.request()

    def showEvent(self, event: QShowEvent) -> None:  # noqa: N802 - API de Qt
        super().showEvent(event)
        self._refresher.shown()

    def show_rows(self, rows: list[DownloadSuggestion]) -> None:
        keep = {r.id for r in self.selected()}
        self._rows = rows
        with filling(self.table):
            self.table.clearSelection()
            self.table.setRowCount(len(rows))
            for i, row in enumerate(rows):
                name = Path(row.path).name + ("" if available(row) else "  (disco no conectado)")
                self.table.setItem(i, 0, cell(name, row.path))
                destination = pretty(row.destination) if row.destination else "—"
                self.table.setItem(i, 1, cell(destination, row.destination))
                confidence = (
                    "—"
                    if row.destination is None or row.source in UNCALIBRATED
                    else f"{row.confidence or 0:.0%}"
                )
                self.table.setItem(i, 2, cell(confidence))
                self.table.setItem(i, 3, cell(explanation(row)))
            selection = self.table.selectionModel()
            for i, row in enumerate(rows):
                if row.id in keep:  # la selección sobrevive a los refrescos
                    selection.select(
                        self.table.model().index(i, 0),
                        QItemSelectionModel.SelectionFlag.Select
                        | QItemSelectionModel.SelectionFlag.Rows,
                    )
        self.table.setVisible(bool(rows))
        self.empty.setVisible(not rows)
        self._update_buttons()

    def selected(self) -> list[DownloadSuggestion]:
        indexes = sorted({i.row() for i in self.table.selectedIndexes()})
        return [self._rows[i] for i in indexes if i < len(self._rows)]

    def _update_buttons(self) -> None:
        chosen = self.selected()
        self.accept.setEnabled(any(r.destination for r in chosen))
        self.move_to.setEnabled(bool(chosen))
        self.ignore.setEnabled(bool(chosen))
        self.open.setEnabled(len(chosen) == 1)

    def _run_each(
        self, rows: list[DownloadSuggestion], op: Callable[[DownloadSuggestion], object]
    ) -> None:
        """Aplica ``op`` a cada fila en segundo plano; los errores se informan al final."""

        def work() -> list[str]:
            errors: list[str] = []
            for row in rows:
                try:
                    op(row)
                except Exception as exc:  # cada archivo es independiente
                    errors.append(f"{Path(row.path).name}: {exc}")
            return errors

        def done(errors: list[str]) -> None:
            self._ctrl.notify_changed()
            if errors:
                self._ctrl.error.emit("\n".join(errors))

        self._ctrl.ops.submit(work, on_done=done, on_error=self._ctrl.report)

    def accept_selected(self) -> None:
        rows = [r for r in self.selected() if r.destination]
        self._run_each(rows, lambda r: self._ctrl.backend.accept(r.id))

    def move_selected(self) -> None:
        rows = self.selected()
        if not rows:
            return
        start = rows[0].destination or str(Path.home())
        folder = self.choose_folder(start)
        if folder is not None:
            self._run_each(rows, lambda r: self._ctrl.backend.accept(r.id, folder))

    def choose_folder(self, start: str) -> Path | None:
        """Selector de carpetas del sistema (portal de KDE en Plasma)."""
        chosen = QFileDialog.getExistingDirectory(self, "Mover a…", start)
        return Path(chosen) if chosen else None

    def open_selected(self) -> None:
        chosen = self.selected()
        if len(chosen) == 1 and (problem := open_path(chosen[0].path)) is not None:
            self._ctrl.error.emit(problem)

    def ignore_selected(self) -> None:
        self._run_each(self.selected(), lambda r: self._ctrl.backend.ignore(r.id))
