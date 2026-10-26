"""Ajustes: carpetas, modo automático, arranque con la sesión y datos."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QListWidget,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from organizador import maintenance
from organizador.app import reload_settings
from organizador.core.config import set_config_value
from organizador.gui.common import heading
from organizador.gui.controller import Controller

MODES = [("suggest", "Solo sugerir"), ("auto", "Mover solo si la confianza es alta")]


class _FolderList(QGroupBox):
    def __init__(self, title: str, hint: str) -> None:
        super().__init__(title)
        layout = QHBoxLayout(self)
        self.list = QListWidget()
        self.list.setToolTip(hint)
        layout.addWidget(self.list)
        buttons = QVBoxLayout()
        add = QPushButton("Añadir…")
        add.clicked.connect(self._add)
        remove = QPushButton("Quitar")
        remove.clicked.connect(self._remove)
        buttons.addWidget(add)
        buttons.addWidget(remove)
        buttons.addStretch()
        layout.addLayout(buttons)

    def set_paths(self, paths: list[Path]) -> None:
        self.list.clear()
        self.list.addItems([str(p) for p in paths])

    def paths(self) -> list[str]:
        return [self.list.item(i).text() for i in range(self.list.count())]

    def add_path(self, path: str) -> None:
        if path and path not in self.paths():
            self.list.addItem(path)

    def _add(self) -> None:
        self.add_path(QFileDialog.getExistingDirectory(self, self.title(), str(Path.home())))

    def _remove(self) -> None:
        for item in self.list.selectedItems():
            self.list.takeItem(self.list.row(item))


class SettingsView(QWidget):
    def __init__(self, ctrl: Controller) -> None:
        super().__init__()
        self._ctrl = ctrl
        layout = QVBoxLayout(self)
        layout.addWidget(heading("Ajustes"))
        self.watched = _FolderList(
            "Carpetas donde llegan descargas",
            "También en discos externos: se vigilan al conectarlos",
        )
        self.indexed = _FolderList(
            "Carpetas organizadas (de ellas aprendo)", "Donde guardas tus archivos ya ordenados"
        )
        self.excluded = _FolderList(
            "Excluidas del aprendizaje",
            "Carpetas que no reflejan tus costumbres (salidas de programas…)",
        )
        for box in (self.watched, self.indexed, self.excluded):
            layout.addWidget(box)

        form = QFormLayout()
        self.mode = QComboBox()
        for _, text in MODES:
            self.mode.addItem(text)
        self.threshold = QSpinBox()
        self.threshold.setRange(50, 100)
        self.threshold.setSuffix(" %")
        self.threshold.setToolTip("Confianza mínima para mover sin preguntar (modo automático)")
        self.mode.currentIndexChanged.connect(lambda i: self.threshold.setEnabled(i == 1))
        form.addRow("Al llegar una descarga:", self.mode)
        form.addRow("Confianza mínima:", self.threshold)
        self.service_autostart = QCheckBox("Vigilar las descargas al iniciar sesión (servicio)")
        self.tray_autostart = QCheckBox("Mostrar el icono en la bandeja al iniciar sesión")
        form.addRow(self.service_autostart)
        form.addRow(self.tray_autostart)
        layout.addLayout(form)

        buttons = QHBoxLayout()
        self.export = QPushButton("Exportar datos…")
        self.export.clicked.connect(self.export_data)
        self.erase = QPushButton("Borrar datos aprendidos…")
        self.erase.clicked.connect(self.erase_data)
        self.save = QPushButton("Guardar")
        self.save.setDefault(True)
        self.save.clicked.connect(self.save_settings)
        buttons.addWidget(self.export)
        buttons.addWidget(self.erase)
        buttons.addStretch()
        buttons.addWidget(self.save)
        layout.addLayout(buttons)
        layout.addStretch()

    def refresh(self) -> None:
        settings = self._ctrl.ctx.settings
        self.watched.set_paths(settings.watched_dirs)
        self.indexed.set_paths(settings.indexed_dirs)
        self.excluded.set_paths([Path(p) for p in settings.learning.exclude])
        self.mode.setCurrentIndex([m for m, _ in MODES].index(settings.watch.mode))
        self.threshold.setValue(round(settings.watch.auto_threshold * 100))
        self.threshold.setEnabled(settings.watch.mode == "auto")
        self._ctrl.runner.submit(
            lambda: (maintenance.service_enabled(), maintenance.tray_autostart_enabled()),
            on_done=self._show_autostart,
        )

    def _show_autostart(self, state: tuple[bool, bool]) -> None:
        self._autostart = state
        self.service_autostart.setChecked(state[0])
        self.tray_autostart.setChecked(state[1])

    def save_settings(self) -> None:
        ctx = self._ctrl.ctx
        values = {
            "watched_dirs": self.watched.paths(),
            "indexed_dirs": self.indexed.paths(),
            "learning.exclude": self.excluded.paths(),
            "watch.mode": MODES[self.mode.currentIndex()][0],
            "watch.auto_threshold": self.threshold.value() / 100,
        }
        service, tray = self.service_autostart.isChecked(), self.tray_autostart.isChecked()
        previous = getattr(self, "_autostart", (service, tray))
        backend = self._ctrl.backend

        def work() -> None:
            for key, value in values.items():
                set_config_value(ctx.paths, key, value)
            reload_settings(ctx)
            if tray != previous[1]:
                maintenance.set_tray_autostart(tray)
            if service != previous[0]:
                maintenance.set_service_autostart(service)
            elif backend.remote:
                backend.reload_settings()  # el servicio vigila las carpetas nuevas

        def done(_r: None) -> None:
            self._autostart = (service, tray)
            self._ctrl.notify_changed()

        self._ctrl.runner.submit(work, on_done=done, on_error=self._ctrl.report)

    def export_data(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Exportar datos a…", str(Path.home()))
        if not folder:
            return
        ctx = self._ctrl.ctx
        self._ctrl.runner.submit(
            lambda: maintenance.export_data(ctx, Path(folder)),
            on_done=lambda path: QMessageBox.information(
                self, "Datos exportados", f"Guardados en\n{path}"
            ),
            on_error=self._ctrl.report,
        )

    def _exported(self, path: Path) -> None:
        QMessageBox.information(self, "Datos exportados", f"Guardados en\n{path}")

    def confirm_erase(self) -> bool:
        answer = QMessageBox.warning(
            self,
            "Borrar datos aprendidos",
            "Se borrarán el índice, el historial, las sugerencias y todo lo aprendido (base de "
            "datos y modelos). Tus archivos y la configuración no se tocan.\n\n"
            "El historial se pierde: después no se podrá deshacer nada de lo anterior.",
            QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        return answer == QMessageBox.StandardButton.Discard

    def erase_data(self) -> None:
        if not self.confirm_erase():
            return
        ctx, remote = self._ctrl.ctx, self._ctrl.remote
        self._ctrl.runner.submit(
            lambda: maintenance.erase_learned_data(ctx, service_running=remote),
            on_done=lambda _r: self._ctrl.notify_changed(),
            on_error=self._ctrl.report,
        )
