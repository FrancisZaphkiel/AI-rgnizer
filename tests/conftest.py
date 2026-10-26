from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def no_session_bus(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ningún test habla con el bus de la sesión real (ni con el servicio instalado).

    Las pruebas del servicio arrancan su propio bus privado.
    """
    monkeypatch.delenv("DBUS_SESSION_BUS_ADDRESS", raising=False)


@pytest.fixture(autouse=True)
def fake_systemctl(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, ...]]:
    """Ningún test toca el gestor de systemd del usuario real: se registran las llamadas.

    ``systemctl --user`` no depende de ``HOME``: aislar el directorio personal no basta.
    """
    from organizador import maintenance
    from organizador.service import systemd

    calls: list[tuple[str, ...]] = []

    def record(*args: str) -> int:
        calls.append(args)
        return 0

    monkeypatch.setattr(systemd, "systemctl", record)
    monkeypatch.setattr(maintenance, "service_active", lambda: False)
    monkeypatch.setattr(maintenance, "service_enabled", lambda: False)
    monkeypatch.setattr(maintenance, "set_service_autostart", lambda enabled: 0)
    return calls


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """``HOME`` temporal sin variables XDG heredadas del usuario real."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    for var in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME"):
        monkeypatch.delenv(var, raising=False)
    return home
