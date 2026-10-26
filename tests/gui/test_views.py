from __future__ import annotations

import time
from pathlib import Path

import pytest
from pytestqt.qtbot import QtBot

from organizador.gui.controller import Controller
from organizador.gui.window import MainWindow
from tests.gui.conftest import idle


def select_row(table: object, row: int) -> None:
    table.clearSelection()  # type: ignore[attr-defined]
    table.selectRow(row)  # type: ignore[attr-defined]


def test_every_view_loads_with_data(qtbot: QtBot, gui: tuple[Controller, MainWindow]) -> None:
    ctrl, window = gui
    for row in range(len(window.views)):
        window.nav.setCurrentRow(row)
        idle(qtbot, ctrl)
        assert window.stack.currentWidget() is window.views[row]
    assert window.downloads.table.rowCount() == 2
    assert window.history.table.rowCount() >= 0
    assert window.patterns.table.rowCount() >= 1
    assert window.settings.watched.paths()


def test_accept_moves_shows_in_history_and_undo_restores(
    qtbot: QtBot, gui: tuple[Controller, MainWindow], data: Path
) -> None:
    ctrl, window = gui
    window.nav.setCurrentRow(1)
    idle(qtbot, ctrl)
    view = window.downloads
    names = [Path(r.path).name for r in view._rows]
    select_row(view.table, names.index("cartola_2024-07.pdf"))
    assert view.accept.isEnabled()
    view.accept.click()
    idle(qtbot, ctrl)
    source = data / "Descargas" / "cartola_2024-07.pdf"
    moved = data / "Documentos" / "Banco" / "cartola_2024-07.pdf"
    assert not source.exists()
    assert moved.exists()
    assert view.table.rowCount() == 1

    window.nav.setCurrentRow(4)
    idle(qtbot, ctrl)
    history = window.history
    rows = [Path(e.destination or "").name for e in history._rows]
    select_row(history.table, rows.index("cartola_2024-07.pdf"))
    history.undo.click()
    idle(qtbot, ctrl)
    assert source.exists()
    assert not moved.exists()


def test_trash_duplicates_asks_first_and_uses_trash(
    qtbot: QtBot, gui: tuple[Controller, MainWindow], data: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctrl, window = gui
    view = window.duplicates
    window.nav.setCurrentRow(2)
    view.search.click()
    idle(qtbot, ctrl)
    assert view.checked_groups()
    group = view.checked_groups()[0]
    redundant = [f.path for f in group.redundant]
    keep = group.keep.path

    asked: list[int] = []
    monkeypatch.setattr(view, "confirm_trash", lambda files, size: asked.append(files) or False)
    view.trash.click()
    idle(qtbot, ctrl)
    assert asked == [len(redundant)]
    assert all(p.exists() for p in redundant)  # cancelado: no se toca nada

    monkeypatch.setattr(view, "confirm_trash", lambda files, size: asked.append(files) or True)
    view.trash.click()
    idle(qtbot, ctrl)
    assert len(asked) == 2
    assert keep.exists()
    trash = data / ".local/share/Trash/files"
    for p in redundant:
        assert not p.exists()
        assert (trash / p.name).exists()  # en la papelera, no borrado


def test_confirm_trash_shows_a_dialog(
    gui: tuple[Controller, MainWindow], monkeypatch: pytest.MonkeyPatch
) -> None:
    from PySide6.QtWidgets import QMessageBox

    calls: list[str] = []

    def question(*args: object) -> QMessageBox.StandardButton:
        calls.append(str(args[1]))
        return QMessageBox.StandardButton.Cancel

    monkeypatch.setattr(QMessageBox, "question", question)
    assert gui[1].duplicates.confirm_trash(2, 10) is False
    assert calls == ["Enviar a la papelera"]


def test_long_scan_does_not_block_the_ui(
    qtbot: QtBot, gui: tuple[Controller, MainWindow], monkeypatch: pytest.MonkeyPatch
) -> None:
    from PySide6.QtCore import QTimer

    from organizador.gui.views import duplicates

    ctrl, window = gui
    real = duplicates.find_duplicates

    def slow(*args: object, **kwargs: object) -> object:
        time.sleep(1.0)
        return real(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(duplicates, "find_duplicates", slow)
    ticks: list[float] = []
    timer = QTimer()
    timer.timeout.connect(lambda: ticks.append(time.monotonic()))
    timer.start(50)
    window.duplicates.search.click()
    assert not window.duplicates.search.isEnabled()
    idle(qtbot, ctrl)
    timer.stop()
    assert len(ticks) >= 10  # el bucle de eventos siguió atendiendo durante el escaneo
    assert window.duplicates.search.isEnabled()


def test_erase_learned_data(
    qtbot: QtBot, gui: tuple[Controller, MainWindow], monkeypatch: pytest.MonkeyPatch
) -> None:
    from sqlalchemy import func, select

    from organizador.storage.models import ActionJournal, DownloadSuggestion

    ctrl, window = gui
    ctx = ctrl.ctx
    view = window.settings
    window.nav.setCurrentRow(1)
    idle(qtbot, ctrl)
    select_row(window.downloads.table, 0)
    window.downloads.ignore.click()
    idle(qtbot, ctrl)
    models = ctx.paths.data_dir / "models"
    models.mkdir(parents=True, exist_ok=True)
    (models / "model-0001.joblib").write_bytes(b"x")

    monkeypatch.setattr(view, "confirm_erase", lambda: False)
    view.erase_data()
    idle(qtbot, ctrl)
    assert (models / "model-0001.joblib").exists()

    monkeypatch.setattr(view, "confirm_erase", lambda: True)
    view.erase_data()
    idle(qtbot, ctrl)
    assert not (models / "model-0001.joblib").exists()
    with ctx.db.session() as s:
        assert s.scalar(select(func.count()).select_from(DownloadSuggestion)) == 0
        assert s.scalar(select(func.count()).select_from(ActionJournal)) == 0
