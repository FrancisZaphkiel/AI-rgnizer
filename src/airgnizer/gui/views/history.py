"""Historial: todas las acciones sobre archivos, con «Deshacer»."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtGui import QShowEvent
from PySide6.QtWidgets import QHBoxLayout, QPushButton, QStyle, QVBoxLayout, QWidget

from airgnizer import overview
from airgnizer.actions.service import UNDOABLE
from airgnizer.gui.common import cell, filling, heading, icon, open_path, pretty, table
from airgnizer.gui.controller import Controller
from airgnizer.storage.models import ActionJournal, ActionKind, ActionState

KIND_TEXT = {
    ActionKind.MOVE: "Mover",
    ActionKind.RENAME: "Renombrar",
    ActionKind.TRASH: "Papelera",
    ActionKind.UNDO: "Deshacer",
}
STATE_TEXT = {
    ActionState.PENDING: "pendiente",
    ActionState.IN_PROGRESS: "en curso",
    ActionState.DONE: "hecho",
    ActionState.FAILED: "falló",
    ActionState.REVERTED: "deshecho",
}


def undoable(entry: ActionJournal) -> bool:
    return entry.kind in UNDOABLE and entry.state == ActionState.DONE


class HistoryView(QWidget):
    def __init__(self, ctrl: Controller) -> None:
        super().__init__()
        self._ctrl = ctrl
        self._rows: list[ActionJournal] = []
        layout = QVBoxLayout(self)
        layout.addWidget(heading("Historial"))
        self.table = table(["Cuándo", "Acción", "Estado", "Archivo", "Destino"], stretch=4)
        self.table.itemSelectionChanged.connect(self._update_buttons)
        self.table.setToolTip("Doble clic: abre el archivo")
        self.table.doubleClicked.connect(lambda index: self.open_row(index.row()))
        layout.addWidget(self.table)
        buttons = QHBoxLayout()
        self.undo = QPushButton(
            icon(self, "edit-undo", QStyle.StandardPixmap.SP_ArrowBack), "Deshacer"
        )
        self.undo.clicked.connect(self.undo_selected)
        buttons.addStretch()
        buttons.addWidget(self.undo)
        layout.addLayout(buttons)
        self._refresher = ctrl.refresher(self, lambda: overview.history(ctrl.ctx), self.show_rows)
        ctrl.changed.connect(self.refresh)
        self._update_buttons()

    def refresh(self) -> None:
        self._refresher.request()

    def showEvent(self, event: QShowEvent) -> None:  # noqa: N802 - API de Qt
        super().showEvent(event)
        self._refresher.shown()

    def open_row(self, row: int) -> None:
        """Abre el archivo donde está ahora: en el destino o, si se deshizo, en el origen."""
        if row >= len(self._rows):
            return
        e = self._rows[row]
        here = e.source if e.state == ActionState.REVERTED or not e.destination else e.destination
        if (problem := open_path(here)) is not None:
            self._ctrl.error.emit(problem)

    def show_rows(self, rows: list[ActionJournal]) -> None:
        self._rows = rows
        with filling(self.table):
            self.table.setRowCount(len(rows))
            for i, e in enumerate(rows):
                when = e.created_at.astimezone().strftime("%d/%m/%Y %H:%M")
                self.table.setItem(i, 0, cell(when))
                self.table.setItem(i, 1, cell(KIND_TEXT.get(e.kind, str(e.kind))))
                state = STATE_TEXT.get(e.state, str(e.state))
                self.table.setItem(i, 2, cell(state, e.error or state))
                self.table.setItem(i, 3, cell(Path(e.source).name, e.source))
                self.table.setItem(
                    i, 4, cell(pretty(e.destination) if e.destination else "—", e.destination)
                )
        self._update_buttons()

    def selected(self) -> list[ActionJournal]:
        indexes = sorted({i.row() for i in self.table.selectedIndexes()})
        return [self._rows[i] for i in indexes if i < len(self._rows)]

    def _update_buttons(self) -> None:
        self.undo.setEnabled(any(undoable(e) for e in self.selected()))

    def undo_selected(self) -> None:
        entries = [e for e in self.selected() if undoable(e)]
        backend = self._ctrl.backend

        def work() -> list[str]:
            errors = []
            for entry in entries:
                try:
                    backend.undo(entry.id)
                except Exception as exc:  # una acción que no se puede deshacer no frena las demás
                    errors.append(f"{Path(entry.source).name}: {exc}")
            return errors

        def done(errors: list[str]) -> None:
            self._ctrl.notify_changed()
            if errors:
                self._ctrl.error.emit("\n".join(errors))

        self._ctrl.ops.submit(work, on_done=done, on_error=self._ctrl.report)
