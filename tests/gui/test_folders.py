from __future__ import annotations

from pathlib import Path

import pytest
from pytestqt.qtbot import QtBot

from organizador.core.config import load_settings
from organizador.gui.controller import Controller
from organizador.gui.window import MainWindow
from organizador.scanner.mounts import MountEntry
from tests.gui.conftest import idle


def test_duplicates_has_its_own_folders_saved_in_config(
    qtbot: QtBot, gui: tuple[Controller, MainWindow], data: Path
) -> None:
    ctrl, window = gui
    window.nav.setCurrentRow(2)
    view = window.duplicates
    assert view.where.paths() == []
    assert view.where.empty.isVisible()  # «se buscan en las carpetas organizadas»

    elsewhere = data / "Disco"
    (elsewhere / "a").mkdir(parents=True)
    (elsewhere / "b").mkdir()
    (elsewhere / "a" / "x.bin").write_bytes(b"igual" * 100)
    (elsewhere / "b" / "y.bin").write_bytes(b"igual" * 100)
    view.where.add_path(str(elsewhere))
    idle(qtbot, ctrl)
    assert load_settings(ctrl.ctx.paths).duplicates.dirs == [str(elsewhere)]
    assert ctrl.ctx.settings.indexed_dirs == [data / "Documentos"]  # aprender: sin cambios

    view.search.click()
    idle(qtbot, ctrl)
    groups = view.checked_groups()
    assert [sorted(f.path.name for f in g.files) for g in groups] == [["x.bin", "y.bin"]]


def test_add_menu_offers_mounted_drives(
    qtbot: QtBot, gui: tuple[Controller, MainWindow], data: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from organizador.gui import folders

    drive = data / "media" / "disco"
    drive.mkdir(parents=True)
    monkeypatch.setattr(folders, "user_mounts", lambda: [MountEntry(drive, "exfat", "/dev/sdb1")])
    asked: list[str] = []

    def choose(_parent: object, _title: str, start: Path | str) -> Path:
        asked.append(str(start))
        return Path(start)

    added: list[Path] = []
    menu = folders.folder_menu(gui[1], "Añadir", Path.home, added.append, choose=choose)
    menu.aboutToShow.emit()
    texts = [a.text() for a in menu.actions()]
    assert any(t.startswith("disco — exfat") for t in texts)
    next(a for a in menu.actions() if a.text().startswith("disco")).trigger()
    assert asked == [str(drive)]  # el diálogo se abre dentro de la unidad
    assert added == [drive]
