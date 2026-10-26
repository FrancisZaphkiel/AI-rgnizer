"""La interfaz sigue respondiendo con muchas sugerencias y ráfagas de cambios."""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from pytestqt.qtbot import QtBot

from airgnizer.gui.controller import Controller
from airgnizer.gui.window import MainWindow
from airgnizer.storage.models import DownloadSuggestion
from tests.gui.conftest import idle


def many_pending(ctrl: Controller, folder: Path, n: int) -> None:
    rows = []
    for i in range(n):
        f = folder / f"archivo_{i:03d}.txt"
        f.write_text(str(i))
        st = f.stat()
        rows.append(
            DownloadSuggestion(
                path=str(f),
                size=st.st_size,
                mtime_ns=st.st_mtime_ns,
                inode=st.st_ino,
                device=st.st_dev,
                destination=str(folder.parent / "Documentos"),
                confidence=None,
                source="tipo",
            )
        )
    with ctrl.ctx.db.session() as s:
        s.add_all(rows)


def test_a_burst_of_changes_is_one_refresh(
    qtbot: QtBot, gui: tuple[Controller, MainWindow]
) -> None:
    ctrl, _ = gui
    idle(qtbot, ctrl)
    emitted: list[int] = []
    ctrl.changed.connect(lambda: emitted.append(1))
    for _ in range(50):  # p. ej. aceptar 50 descargas: 50 señales del servicio
        ctrl.notify_changed()
    idle(qtbot, ctrl)
    assert emitted == [1]


def test_hidden_views_reload_only_when_shown(
    qtbot: QtBot, gui: tuple[Controller, MainWindow], data: Path
) -> None:
    ctrl, window = gui
    idle(qtbot, ctrl)
    many_pending(ctrl, data / "Descargas", 3)
    ctrl.notify_changed()
    idle(qtbot, ctrl)
    assert window.downloads.table.rowCount() == 0  # oculta: no se consultó
    window.nav.setCurrentRow(1)
    idle(qtbot, ctrl)
    assert window.downloads.table.rowCount() == 5


def test_event_loop_keeps_running_with_hundreds_of_pending(
    qtbot: QtBot, gui: tuple[Controller, MainWindow], data: Path
) -> None:
    from PySide6.QtCore import QTimer

    ctrl, window = gui
    many_pending(ctrl, data / "Descargas", 450)
    window.nav.setCurrentRow(1)
    idle(qtbot, ctrl)
    assert window.downloads.table.rowCount() == 452

    gaps: list[float] = []
    last = [time.monotonic()]

    def tick() -> None:
        now = time.monotonic()
        gaps.append(now - last[0])
        last[0] = now

    timer = QTimer()
    timer.timeout.connect(tick)
    timer.start(10)
    end = time.monotonic() + 1.5
    while time.monotonic() < end:  # una señal del servicio cada 15 ms
        ctrl.notify_changed()
        qtbot.wait(15)
    idle(qtbot, ctrl)
    timer.stop()
    assert max(gaps) < 0.5, f"la interfaz se detuvo {max(gaps):.2f} s"


def test_selection_survives_a_refresh(qtbot: QtBot, gui: tuple[Controller, MainWindow]) -> None:
    ctrl, window = gui
    window.nav.setCurrentRow(1)
    idle(qtbot, ctrl)
    view = window.downloads
    view.table.selectRow(1)
    chosen = view.selected()[0].id
    view.refresh()
    idle(qtbot, ctrl)
    assert [r.id for r in view.selected()] == [chosen]


def test_double_click_opens_instead_of_accepting(
    qtbot: QtBot, gui: tuple[Controller, MainWindow], data: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from PySide6.QtGui import QDesktopServices

    opened: list[str] = []
    monkeypatch.setattr(
        QDesktopServices, "openUrl", lambda url: opened.append(url.toLocalFile()) or True
    )
    ctrl, window = gui
    window.nav.setCurrentRow(1)
    idle(qtbot, ctrl)
    view = window.downloads
    view.table.selectRow(0)
    path = view.selected()[0].path
    view.table.doubleClicked.emit(view.table.model().index(0, 0))
    idle(qtbot, ctrl)
    assert opened == [path]
    assert Path(path).exists()  # no se movió
    assert view.table.rowCount() == 2

    view.open.click()
    assert opened == [path, path]


def test_open_reports_missing_file(gui: tuple[Controller, MainWindow], data: Path) -> None:
    from airgnizer.gui.common import open_path

    assert "Ya no existe" in (open_path(data / "no-esta.pdf") or "")
