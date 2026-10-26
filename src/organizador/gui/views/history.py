"""Historial: todas las acciones sobre archivos, con «Deshacer»."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import QHBoxLayout, QPushButton, QStyle, QVBoxLayout, QWidget

from organizador import overview
from organizador.actions.service import UNDOABLE
from organizador.gui.common import cell, heading, icon, pretty, table
from organizador.gui.controller import Controller
from organizador.storage.models import ActionJournal, ActionKind, ActionState

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
        layout.addWidget(self.table)
        buttons = QHBoxLayout()
        self.undo = QPushButton(
            icon(self, "edit-undo", QStyle.StandardPixmap.SP_ArrowBack), "Deshacer"
        )
        self.undo.clicked.connect(self.undo_selected)
        buttons.addStretch()
        buttons.addWidget(self.undo)
        layout.addLayout(buttons)
        ctrl.changed.connect(self.refresh)
        self._update_buttons()

    def refresh(self) -> None:
        self._ctrl.runner.submit(
            lambda: overview.history(self._ctrl.ctx),
            on_done=self.show_rows,
            on_error=self._ctrl.report,
        )

    def show_rows(self, rows: list[ActionJournal]) -> None:
        self._rows = rows
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

        self._ctrl.runner.submit(work, on_done=done, on_error=self._ctrl.report)
