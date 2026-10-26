"""Duplicados: grupos de copias exactas, la copia a conservar y envío a la papelera.

Nunca se borra nada: las copias sobrantes van a la papelera del sistema, siempre tras
una confirmación, y cada una queda en el historial para deshacerla.
"""

from __future__ import annotations

import contextlib
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QIcon, QImage, QImageReader, QPixmap
from PySide6.QtWidgets import (
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QStyle,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from organizador.app import AppContext, reload_settings
from organizador.core.config import set_config_value
from organizador.duplicates import DuplicateFinder, DuplicateGroup, KeepPolicy, trash_redundant
from organizador.duplicates.finder import DuplicateReport
from organizador.gui.common import heading, human_size, icon, open_path, pretty
from organizador.gui.controller import Controller
from organizador.gui.folders import FolderList
from organizador.scanner import Scanner

THUMB = 48
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tif", ".tiff", ".heic"}
_ROLE = Qt.ItemDataRole.UserRole


def find_duplicates(
    ctx: AppContext, cancel: threading.Event, progress: Callable[[str], None]
) -> DuplicateReport:
    """Reescanea las carpetas indexadas y busca duplicados exactos (en segundo plano)."""
    roots = [r for r in ctx.settings.duplicate_roots() if r.is_dir()]
    scanner = Scanner(ctx.db, ctx.settings.scan)
    for root in roots:
        if cancel.is_set():
            break
        scanner.scan(
            root,
            cancel=cancel,
            progress=lambda st: progress(f"Escaneando: {st.files_seen} archivos…"),
        )
    policy = KeepPolicy(ctx.settings.indexed_dirs, ctx.settings.watched_dirs)
    return DuplicateFinder(ctx.db, ctx.settings.duplicates, policy).find(
        roots,
        cancel=cancel,
        progress=lambda st: progress(
            f"Comparando contenido: {human_size(st.bytes_hashed)} leídos…"
        ),
    )


def thumbnail(path: Path) -> QImage | None:
    """Miniatura reducida al leer (no carga la imagen entera); en segundo plano."""
    if path.suffix.lower() not in IMAGE_SUFFIXES:
        return None
    reader = QImageReader(str(path))
    reader.setAutoTransform(True)
    size = reader.size()
    if size.isValid():
        reader.setScaledSize(size.scaled(THUMB, THUMB, Qt.AspectRatioMode.KeepAspectRatio))
    image = reader.read()
    return None if image.isNull() else image


@dataclass(slots=True)
class _Shown:
    group: DuplicateGroup
    item: QTreeWidgetItem


class DuplicatesView(QWidget):
    def __init__(self, ctrl: Controller) -> None:
        super().__init__()
        self._ctrl = ctrl
        self._shown: list[_Shown] = []
        self._cancel: threading.Event | None = None
        layout = QVBoxLayout(self)
        layout.addWidget(heading("Duplicados"))
        intro = QLabel(
            "Copias exactas (mismo contenido) en tus carpetas indexadas. Marca con ★ la copia "
            "que quieres conservar; las demás irán a la papelera, de donde se pueden recuperar."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)
        self.where = FolderList(
            "Buscar duplicados en",
            "Carpetas y unidades donde buscar copias; se guardan al cambiarlas",
            "Ninguna elegida: se buscan en las carpetas organizadas de Ajustes.",
        )
        self.where.setMaximumHeight(150)
        self.where.changed.connect(self._save_where)
        layout.addWidget(self.where)
        top = QHBoxLayout()
        self.search = QPushButton(
            icon(self, "edit-find", QStyle.StandardPixmap.SP_FileDialogContentsView),
            "Buscar duplicados",
        )
        self.search.clicked.connect(self.start_search)
        self.stop = QPushButton("Cancelar")
        self.stop.clicked.connect(self.cancel_search)
        self.stop.setVisible(False)
        self.status = QLabel("")
        top.addWidget(self.search)
        top.addWidget(self.stop)
        top.addWidget(self.status, 1)
        layout.addLayout(top)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Archivo", "Tamaño", "Carpeta"])
        self.tree.setIconSize(QSize(THUMB, THUMB))
        self.tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.tree.header().setStretchLastSection(True)
        self.tree.itemSelectionChanged.connect(self._update_buttons)
        self.tree.itemChanged.connect(lambda *_: self._update_buttons())
        self.tree.itemDoubleClicked.connect(self._open_item)
        layout.addWidget(self.tree)

        bottom = QHBoxLayout()
        self.keep = QPushButton("★ Conservar esta")
        self.keep.setToolTip("Conserva la copia seleccionada en lugar de la recomendada")
        self.keep.clicked.connect(self.keep_selected)
        self.trash = QPushButton(
            icon(self, "user-trash", QStyle.StandardPixmap.SP_TrashIcon), "Enviar a papelera"
        )
        self.trash.clicked.connect(self.trash_checked)
        bottom.addStretch()
        bottom.addWidget(self.keep)
        bottom.addWidget(self.trash)
        layout.addLayout(bottom)
        self._update_buttons()

    def refresh(self) -> None:
        self.where.set_paths(self._ctrl.ctx.settings.duplicates.dirs)

    def _save_where(self) -> None:
        ctx, dirs = self._ctrl.ctx, self.where.paths()

        def work() -> None:
            set_config_value(ctx.paths, "duplicates.dirs", dirs)
            reload_settings(ctx)

        self._ctrl.ops.submit(
            work, on_done=lambda _r: self._ctrl.notify_changed(), on_error=self._ctrl.report
        )

    # --- búsqueda ----------------------------------------------------------------------

    def start_search(self) -> None:
        if not self._ctrl.ctx.settings.duplicate_roots():
            self.status.setText("Añade arriba las carpetas o unidades donde buscar.")
            return
        self._cancel = cancel = threading.Event()
        self.search.setEnabled(False)
        self.stop.setVisible(True)
        self.status.setText("Buscando…")
        ctx = self._ctrl.ctx
        self._ctrl.jobs.submit(
            lambda progress: find_duplicates(ctx, cancel, progress),
            on_done=self._found,
            on_error=self._failed,
            on_progress=self.status.setText,
        )

    def cancel_search(self) -> None:
        if self._cancel is not None:
            self._cancel.set()
            self.status.setText("Cancelando…")

    def _finish(self) -> None:
        self.search.setEnabled(True)
        self.stop.setVisible(False)
        self._cancel = None

    def _failed(self, exc: Exception) -> None:
        self._finish()
        self.status.setText("")
        self._ctrl.report(exc)

    def _found(self, report: DuplicateReport) -> None:
        cancelled = self._cancel is not None and self._cancel.is_set()
        self._finish()
        self.show_groups(report.groups)
        freed = human_size(report.recoverable_bytes)
        prefix = "Búsqueda cancelada: resultados parciales. " if cancelled else ""
        self.status.setText(f"{prefix}{len(report.groups)} grupos · se pueden liberar {freed}")
        self._ctrl.notify_changed()  # el panel muestra el espacio recuperable nuevo

    # --- grupos ------------------------------------------------------------------------

    def show_groups(self, groups: list[DuplicateGroup]) -> None:
        self.tree.blockSignals(True)
        self.tree.clear()
        self._shown = []
        for group in groups:
            item = QTreeWidgetItem(self.tree)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(0, Qt.CheckState.Checked)
            self._shown.append(_Shown(group, item))
            self._fill(len(self._shown) - 1)
            item.setExpanded(True)
        self.tree.blockSignals(False)
        self._load_thumbnails()
        self._update_buttons()

    def _fill(self, index: int) -> None:
        shown = self._shown[index]
        group, item = shown.group, shown.item
        item.setText(
            0,
            f"{len(group.files)} copias · libera {human_size(group.recoverable_bytes)}",
        )
        item.setText(1, human_size(group.size))
        item.takeChildren()
        for f in group.files:
            child = QTreeWidgetItem(item)
            is_keep = f.inode_key == group.keep.inode_key
            child.setText(0, ("★ " if is_keep else "") + f.path.name)
            child.setText(1, human_size(f.size))
            child.setText(2, pretty(f.path.parent))
            child.setToolTip(2, str(f.path))
            child.setToolTip(
                0, f"Se conserva: {group.keep_reason}" if is_keep else "Va a la papelera"
            )
            child.setData(0, _ROLE, (index, f))
            font = child.font(0)
            font.setBold(is_keep)
            child.setFont(0, font)

    def _load_thumbnails(self) -> None:
        targets: list[tuple[QTreeWidgetItem, Path]] = []
        for shown in self._shown:
            for i, f in enumerate(shown.group.files):
                child = shown.item.child(i)
                if child is not None and f.path.suffix.lower() in IMAGE_SUFFIXES:
                    targets.append((child, f.path))
        if not targets:
            return
        paths = [p for _, p in targets]

        def load() -> list[QImage | None]:
            return [thumbnail(p) for p in paths]

        def show(images: list[QImage | None]) -> None:
            for (child, _), image in zip(targets, images, strict=True):
                if image is not None:
                    # RuntimeError: la lista cambió mientras se cargaban.
                    with contextlib.suppress(RuntimeError):
                        child.setIcon(0, QIcon(QPixmap.fromImage(image)))

        self._ctrl.jobs.submit(load, on_done=show)

    def _selected_file(self) -> tuple[int, Any] | None:
        items = self.tree.selectedItems()
        data = items[0].data(0, _ROLE) if items else None
        return data if isinstance(data, tuple) else None

    def _open_item(self, item: QTreeWidgetItem, _column: int) -> None:
        data = item.data(0, _ROLE)
        if isinstance(data, tuple) and (problem := open_path(data[1].path)) is not None:
            self._ctrl.error.emit(problem)

    def checked_groups(self) -> list[DuplicateGroup]:
        return [s.group for s in self._shown if s.item.checkState(0) == Qt.CheckState.Checked]

    def _update_buttons(self) -> None:
        self.keep.setEnabled(self._selected_file() is not None)
        self.trash.setEnabled(bool(self.checked_groups()))

    def keep_selected(self) -> None:
        selected = self._selected_file()
        if selected is None:
            return
        index, chosen = selected
        self.tree.blockSignals(True)
        self._shown[index].group = self._shown[index].group.with_keep(chosen)
        self._fill(index)
        self.tree.blockSignals(False)
        self._load_thumbnails()

    def confirm_trash(self, files: int, size: int) -> bool:
        """Confirmación obligatoria antes de enviar nada a la papelera."""
        answer = QMessageBox.question(
            self,
            "Enviar a la papelera",
            f"Se enviarán {files} archivos ({human_size(size)}) a la papelera.\n\n"
            "Se conserva la copia marcada con ★ de cada grupo. Podrás recuperarlos desde la "
            "papelera o con «Deshacer» en el historial.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        return answer == QMessageBox.StandardButton.Yes

    def trash_checked(self) -> None:
        groups = self.checked_groups()
        if not groups:
            return
        files = sum(len(g.redundant) for g in groups)
        size = sum(g.recoverable_bytes for g in groups)
        if not self.confirm_trash(files, size):
            return
        backend = self._ctrl.backend

        def work() -> tuple[int, list[str]]:
            trashed, errors = 0, []
            for group in groups:
                try:
                    result = trash_redundant(group, backend)
                except Exception as exc:  # el grupo entero se aborta si la copia a conservar cambió
                    errors.append(f"{group.keep.path.name}: {exc}")
                    continue
                trashed += len(result.trashed)
                errors += [f"{f.path.name}: {e}" for f, e in result.failed]
            return trashed, errors

        def done(outcome: tuple[int, list[str]]) -> None:
            trashed, errors = outcome
            gone = {id(g) for g in groups}
            self.show_groups([s.group for s in self._shown if id(s.group) not in gone])
            self.status.setText(f"{trashed} archivos enviados a la papelera.")
            self._ctrl.notify_changed()
            if errors:
                self._ctrl.error.emit("\n".join(errors))

        self._ctrl.ops.submit(work, on_done=done, on_error=self._ctrl.report)
