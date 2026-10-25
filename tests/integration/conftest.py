from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from organizador.actions import ActionService
from organizador.core.config import ActionSettings
from organizador.storage import Database


@pytest.fixture
def xdg_data(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Papelera del home aislada dentro de tmp_path (mismo sistema de archivos)."""
    data = tmp_path / "xdg-data"
    data.mkdir()
    monkeypatch.setenv("XDG_DATA_HOME", str(data))
    return data


@pytest.fixture
def db(tmp_path: Path) -> Iterator[Database]:
    database = Database(tmp_path / "test.db")
    database.migrate()
    yield database
    database.dispose()


@pytest.fixture
def service(db: Database, tmp_path: Path, xdg_data: Path) -> ActionService:
    return ActionService(db, ActionSettings(), tmp_path / "state" / "actions.lock", origin="test")


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    root = tmp_path / "work"
    (root / "Descargas").mkdir(parents=True)
    (root / "Documentos").mkdir()
    return root
