"""Patrones: cómo nombras los archivos de cada carpeta; la plantilla se puede editar."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QAbstractItemView, QLabel, QTableWidgetItem, QVBoxLayout, QWidget

from organizador import overview
from organizador.gui.common import cell, heading, pretty, table
from organizador.gui.controller import Controller

COL_FOLDER, COL_TEMPLATE, COL_SUPPORT, COL_EXAMPLES = range(4)


class PatternsView(QWidget):
    def __init__(self, ctrl: Controller) -> None:
        super().__init__()
        self._ctrl = ctrl
        self._rows: list[overview.FolderPatternRow] = []
        layout = QVBoxLayout(self)
        layout.addWidget(heading("Patrones de nombres"))
        intro = QLabel(
            "Plantilla que siguen los nombres de cada carpeta; se usa para proponer el nombre "
            "de lo que llega. Haz doble clic en una plantilla para cambiarla y desmarca la "
            "casilla para no usarla. Campos: {fecha:AAAA-MM-DD}, {texto}, {n}."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)
        self.table = table(["Carpeta", "Plantilla", "Siguen el patrón", "Ejemplos"], stretch=3)
        self.table.setEditTriggers(
            QAbstractItemView.EditTrigger.DoubleClicked
            | QAbstractItemView.EditTrigger.EditKeyPressed
        )
        self.table.itemChanged.connect(self._edited)
        layout.addWidget(self.table)
        self.empty = QLabel(
            "Aún no hay carpetas con un patrón claro (indexa tus carpetas en Ajustes)."
        )
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.empty)

    def refresh(self) -> None:
        self._ctrl.runner.submit(
            lambda: overview.pattern_rows(self._ctrl.ctx),
            on_done=self.show_rows,
            on_error=self._ctrl.report,
        )

    def show_rows(self, rows: list[overview.FolderPatternRow]) -> None:
        self._rows = rows
        self.table.blockSignals(True)
        self.table.setRowCount(len(rows))
        for i, row in enumerate(rows):
            folder = cell(pretty(row.folder), row.folder)
            folder.setFlags(folder.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            folder.setCheckState(Qt.CheckState.Checked if row.enabled else Qt.CheckState.Unchecked)
            self.table.setItem(i, COL_FOLDER, folder)
            template = QTableWidgetItem(row.pattern.pattern.template if row.pattern else "")
            template.setFlags(template.flags() | Qt.ItemFlag.ItemIsEditable)
            template.setToolTip("Editada por ti" if row.edited else "Inferida de tus archivos")
            if row.edited:
                font = template.font()
                font.setItalic(True)
                template.setFont(font)
            self.table.setItem(i, COL_TEMPLATE, template)
            p = row.pattern
            support = f"{p.support} de {p.total} ({p.fraction:.0%})" if p and p.total else "—"
            self.table.setItem(i, COL_SUPPORT, cell(support))
            self.table.setItem(i, COL_EXAMPLES, cell(", ".join(p.examples) if p else ""))
        self.table.blockSignals(False)
        self.table.setVisible(bool(rows))
        self.empty.setVisible(not rows)

    def _edited(self, item: QTableWidgetItem) -> None:
        if item.column() not in (COL_FOLDER, COL_TEMPLATE) or item.row() >= len(self._rows):
            return
        row = self._rows[item.row()]
        folder_item = self.table.item(item.row(), COL_FOLDER)
        template_item = self.table.item(item.row(), COL_TEMPLATE)
        if folder_item is None or template_item is None:
            return
        enabled = folder_item.checkState() == Qt.CheckState.Checked
        template = template_item.text()
        backend = self._ctrl.backend
        self._ctrl.runner.submit(
            lambda: backend.set_pattern(row.folder, template, enabled),
            on_done=lambda _r: self.refresh(),
            on_error=self._rejected,
        )

    def _rejected(self, exc: Exception) -> None:
        self._ctrl.report(exc)
        self.refresh()  # vuelve a mostrar lo guardado
