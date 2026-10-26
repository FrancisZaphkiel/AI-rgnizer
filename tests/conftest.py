from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def no_session_bus(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ningún test habla con el bus de la sesión real (ni con el servicio instalado).

    Las pruebas del servicio arrancan su propio bus privado.
    """
    monkeypatch.delenv("DBUS_SESSION_BUS_ADDRESS", raising=False)


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """``HOME`` temporal sin variables XDG heredadas del usuario real."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    for var in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME"):
        monkeypatch.delenv(var, raising=False)
    return home
