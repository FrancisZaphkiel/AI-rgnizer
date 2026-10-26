"""Panel: resumen de un vistazo."""

from __future__ import annotations

import sys
from dataclasses import dataclass

from PySide6.QtCore import QProcess, Signal
from PySide6.QtGui import QShowEvent
from PySide6.QtWidgets import QFrame, QGridLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from airgnizer import overview
from airgnizer.gui.common import heading, human_size
from airgnizer.gui.controller import Controller
from airgnizer.learning.store import ModelInfo

# Código de salida de «airgnizer train» cuando faltan ejemplos.
NOT_ENOUGH_DATA = 1


@dataclass(frozen=True, slots=True)
class Summary:
    pending: int
    recoverable: overview.Recoverable
    accuracy: overview.Accuracy
    model: ModelInfo | None


def summarize(ctrl: Controller) -> Summary:
    ctx = ctrl.ctx
    return Summary(
        len(overview.pending_suggestions(ctx)),
        overview.recoverable_from_index(ctx),
        overview.suggestion_accuracy(ctx),
        overview.model_info(ctx),
    )


class _Card(QFrame):
    def __init__(self, title: str) -> None:
        super().__init__()
        self.setFrameShape(QFrame.Shape.StyledPanel)
        layout = QVBoxLayout(self)
        caption = QLabel(title)
        caption.setEnabled(False)  # tono secundario del tema
        self.box = layout
        self.value = QLabel("…")
        font = self.value.font()
        font.setPointSizeF(font.pointSizeF() * 1.8)
        font.setBold(True)
        self.value.setFont(font)
        self.detail = QLabel("")
        self.detail.setWordWrap(True)
        for w in (caption, self.value, self.detail):
            layout.addWidget(w)
        layout.addStretch()


class PanelView(QWidget):
    open_downloads = Signal()
    open_duplicates = Signal()

    def __init__(self, ctrl: Controller) -> None:
        super().__init__()
        self._ctrl = ctrl
        layout = QVBoxLayout(self)
        layout.addWidget(heading("Panel"))
        grid = QGridLayout()
        self.pending = _Card("Descargas pendientes")
        self.space = _Card("Espacio recuperable")
        self.accuracy = _Card("Acierto de las sugerencias")
        self.model = _Card("Modelo")
        for i, card in enumerate((self.pending, self.space, self.accuracy, self.model)):
            grid.addWidget(card, i // 2, i % 2)
        layout.addLayout(grid)
        see_pending = QPushButton("Revisar descargas")
        see_pending.clicked.connect(self.open_downloads.emit)
        see_dupes = QPushButton("Buscar duplicados")
        see_dupes.clicked.connect(self.open_duplicates.emit)
        self.learn = QPushButton("Aprender de mis carpetas")
        self.learn.setToolTip("Reescanea las carpetas organizadas y vuelve a entrenar el modelo")
        self.learn.clicked.connect(self.train)
        self.pending.box.addWidget(see_pending)
        self.space.box.addWidget(see_dupes)
        self.model.box.addWidget(self.learn)
        self.service = QLabel("")
        layout.addWidget(self.service)
        layout.addStretch()
        self._training: QProcess | None = None
        self._refresher = ctrl.refresher(self, lambda: summarize(ctrl), self.show_summary)
        ctrl.changed.connect(self.refresh)

    def train(self) -> None:
        """Reescanea y reentrena en otro proceso: no compite con la interfaz por el GIL."""
        ctx = self._ctrl.ctx
        if not ctx.settings.indexed_dirs:
            self._ctrl.error.emit(
                "Configura en Ajustes las carpetas organizadas de las que aprender."
            )
            return
        if self._training is not None:
            return
        self.learn.setEnabled(False)
        self.learn.setText("Aprendiendo…")
        process = self._training = QProcess(self)
        process.finished.connect(self._trained)
        process.start(sys.executable, ["-m", "airgnizer", "train"])

    def _trained(self, code: int, status: QProcess.ExitStatus) -> None:
        process, self._training = self._training, None
        self.learn.setEnabled(True)
        self.learn.setText("Aprender de mis carpetas")
        if process is None:
            return
        errors = bytes(process.readAllStandardError().data()).decode(errors="replace").strip()
        process.deleteLater()
        if status == QProcess.ExitStatus.NormalExit and code == NOT_ENOUGH_DATA:
            self._ctrl.error.emit(
                "Aún no hay suficientes ejemplos: hacen falta al menos dos carpetas con "
                f"{self._ctrl.ctx.settings.learning.min_examples} archivos o más."
            )
        elif status != QProcess.ExitStatus.NormalExit or code != 0:
            self._ctrl.error.emit(errors or "No se pudo entrenar el modelo.")
        self._ctrl.notify_changed()

    def refresh(self) -> None:
        self._refresher.request()

    def showEvent(self, event: QShowEvent) -> None:  # noqa: N802 - API de Qt
        super().showEvent(event)
        self._refresher.shown()

    def show_summary(self, summary: Summary) -> None:
        self.pending.value.setText(str(summary.pending))
        self.pending.detail.setText(
            "Llegaron a tus carpetas vigiladas y esperan destino."
            if summary.pending
            else "Todo en orden."
        )
        rec = summary.recoverable
        self.space.value.setText(human_size(rec.bytes))
        self.space.detail.setText(
            f"En {rec.groups} grupos de duplicados exactos (según la última búsqueda)."
        )
        acc = summary.accuracy
        if acc.fraction is None:
            self.accuracy.value.setText("—")
            self.accuracy.detail.setText("Aún no has respondido a ninguna sugerencia.")
        else:
            self.accuracy.value.setText(f"{acc.fraction:.0%}")
            self.accuracy.detail.setText(
                f"Aceptaste {acc.accepted} de las últimas {acc.total} sugerencias tal cual."
            )
        model = summary.model
        if model is None:
            self.model.value.setText("Sin entrenar")
            self.model.detail.setText("Se usan tus reglas y los destinos por tipo.")
        else:
            when = model.trained_at.astimezone().strftime("%d/%m/%Y %H:%M")
            self.model.value.setText(f"Versión {model.version}")
            self.model.detail.setText(
                f"{model.n_examples} ejemplos, {model.n_classes} carpetas, "
                f"{model.n_rules} reglas · {when}"
            )
        self.service.setText(
            "Servicio en marcha: vigila tus descargas aunque cierres esta ventana."
            if self._ctrl.remote
            else "El servicio no está en marcha: actívalo en Ajustes para vigilar las descargas."
        )
