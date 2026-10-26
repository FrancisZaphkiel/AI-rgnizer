"""Paso de «organizador» (≤ 0.3.0) a AI-rgnizer: nada se pierde ni se queda a medias."""

from __future__ import annotations

import fcntl
import os
from pathlib import Path

import pytest

from airgnizer import desktop, launcher
from airgnizer.actions import fsops
from airgnizer.core.legacy import LegacyServiceRunningError, migrate
from airgnizer.core.paths import AppPaths
from airgnizer.upgrade import upgrade


def legacy_install(home: Path) -> None:
    (home / ".config/organizador").mkdir(parents=True)
    (home / ".config/organizador/config.toml").write_text('watched_dirs = ["/x"]\n')
    data = home / ".local/share/organizador"
    data.mkdir(parents=True)
    for suffix in ("", "-wal", "-shm"):
        (data / f"organizador.db{suffix}").write_bytes(b"db" + suffix.encode())
    (home / ".local/state/organizador").mkdir(parents=True)
    (home / ".local/state/organizador/daemon.lock").touch()


def test_migrate_renames_folders_and_database(isolated_home: Path) -> None:
    legacy_install(isolated_home)
    paths = AppPaths.from_env()
    moved = migrate(paths)
    assert paths.config_file.read_text() == 'watched_dirs = ["/x"]\n'
    assert paths.database_file.read_bytes() == b"db"
    assert (paths.data_dir / "airgnizer.db-wal").read_bytes() == b"db-wal"
    assert not (isolated_home / ".local/share/organizador").exists()
    assert len(moved) == 6
    assert migrate(paths) == []  # la segunda vez no hay nada que hacer


def test_migrate_refuses_while_the_old_service_runs(isolated_home: Path) -> None:
    legacy_install(isolated_home)
    lock = isolated_home / ".local/state/organizador/daemon.lock"
    fd = os.open(lock, os.O_RDWR)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        with pytest.raises(LegacyServiceRunningError, match="systemctl --user disable"):
            migrate(AppPaths.from_env())
    finally:
        os.close(fd)
    assert (isolated_home / ".local/share/organizador/organizador.db").exists()  # intacto


def test_existing_new_data_is_never_overwritten(isolated_home: Path) -> None:
    legacy_install(isolated_home)
    paths = AppPaths.from_env()
    paths.ensure()
    paths.config_file.write_text("# nuevo\n")
    migrate(paths)
    assert paths.config_file.read_text() == "# nuevo\n"
    assert (isolated_home / ".config/organizador/config.toml").exists()


def test_upgrade_retires_old_service_and_restores_it_with_the_new_name(
    isolated_home: Path, fake_systemctl: list[tuple[str, ...]], monkeypatch: pytest.MonkeyPatch
) -> None:
    from airgnizer import maintenance

    enabled: list[bool] = []
    monkeypatch.setattr(maintenance, "set_service_autostart", lambda on: enabled.append(on) or 0)
    legacy_install(isolated_home)
    unit = isolated_home / ".config/systemd/user/organizador.service"
    unit.parent.mkdir(parents=True)
    unit.write_text("[Service]\n")
    autostart = isolated_home / ".config/autostart/io.github.organizador.Organizador.desktop"
    autostart.parent.mkdir(parents=True)
    autostart.write_text("[Desktop Entry]\n")

    report = upgrade(AppPaths.from_env())

    assert ("disable", "--now", "organizador.service") in fake_systemctl
    assert enabled == [True]  # estaba activado: se activa con el nombre nuevo
    assert not unit.exists()
    assert not autostart.exists()
    new_autostart = isolated_home / ".config/autostart/io.github.airgnizer.AIrgnizer.desktop"
    assert "gui --hidden" in new_autostart.read_text()
    assert report.service_restored and report.tray_restored and report.moved


def test_nothing_to_upgrade_touches_nothing(
    isolated_home: Path, fake_systemctl: list[tuple[str, ...]]
) -> None:
    assert not upgrade(AppPaths.from_env())
    assert fake_systemctl == []


def test_from_an_appimage_the_service_and_menu_call_the_image(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    image = isolated_home / "Aplicaciones/AI-rgnizer-x86_64.AppImage"
    image.parent.mkdir()
    image.write_bytes(b"\x7fELF")
    monkeypatch.setenv("APPIMAGE", str(image))
    assert launcher.self_command("daemon") == [str(image), "daemon"]
    desktop.install()
    assert f"Exec={image} gui" in desktop.menu_entry().read_text()
    assert desktop.up_to_date()
    assert desktop.icon_file().read_text().startswith("<svg")

    moved = image.with_name("otra.AppImage")
    image.rename(moved)
    monkeypatch.setenv("APPIMAGE", str(moved))
    assert not desktop.up_to_date()  # la AppImage se movió: hay que reescribir la entrada

    assert desktop.uninstall() == [desktop.menu_entry(), desktop.icon_file()]
    assert not desktop.menu_entry().exists()


def test_menu_remover_refuses_other_files(tmp_path: Path) -> None:
    other = tmp_path / "applications" / "firefox.desktop"
    other.parent.mkdir()
    other.write_text("x")
    with pytest.raises(ValueError, match="No es la entrada"):
        fsops.remove_menu_entry(other)
    assert other.exists()


def test_moving_the_appimage_rewrites_the_installed_unit(
    isolated_home: Path, fake_systemctl: list[tuple[str, ...]], monkeypatch: pytest.MonkeyPatch
) -> None:
    from airgnizer.service import systemd

    old = isolated_home / "Descargas/AI-rgnizer.AppImage"
    new = isolated_home / "Aplicaciones/AI-rgnizer.AppImage"
    for image in (old, new):
        image.parent.mkdir(parents=True, exist_ok=True)
        image.write_bytes(b"\x7fELF")
    monkeypatch.setenv("APPIMAGE", str(old))
    assert not systemd.refresh_if_moved()  # no estaba instalado: no se instala
    systemd.install()
    assert not systemd.refresh_if_moved()  # al día

    monkeypatch.setenv("APPIMAGE", str(new))
    assert systemd.refresh_if_moved()
    assert f"ExecStart={new} daemon" in systemd.unit_path().read_text()
    assert ("try-restart", "airgnizer.service") in fake_systemctl
