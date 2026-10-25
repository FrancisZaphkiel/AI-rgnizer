from __future__ import annotations

import stat
from pathlib import Path

import pytest

from organizador.core import paths as xdg
from organizador.core.config import Settings, load_settings, save_settings
from organizador.core.paths import AppPaths


def test_default_xdg_paths(isolated_home: Path) -> None:
    app = AppPaths.from_env()
    assert app.config_dir == isolated_home / ".config/organizador"
    assert app.data_dir == isolated_home / ".local/share/organizador"
    assert app.state_dir == isolated_home / ".local/state/organizador"
    assert app.cache_dir == isolated_home / ".cache/organizador"


def test_ensure_creates_private_dirs(isolated_home: Path) -> None:
    app = AppPaths.from_env()
    app.ensure()
    for directory in (app.config_dir, app.data_dir, app.state_dir, app.cache_dir):
        assert directory.is_dir()
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700


def test_xdg_env_override_and_relative_ignored(
    isolated_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", "relativo/no/valido")
    assert xdg.data_home() == tmp_path / "data"
    assert xdg.config_home() == isolated_home / ".config"


def test_downloads_dir_from_user_dirs(isolated_home: Path) -> None:
    cfg = isolated_home / ".config"
    cfg.mkdir()
    (cfg / "user-dirs.dirs").write_text('XDG_DOWNLOAD_DIR="$HOME/Descargas"\n', encoding="utf-8")
    assert xdg.downloads_dir() == isolated_home / "Descargas"


def test_downloads_dir_fallback(isolated_home: Path) -> None:
    assert xdg.downloads_dir() == isolated_home / "Downloads"


def test_settings_roundtrip(isolated_home: Path) -> None:
    app = AppPaths.from_env()
    settings = Settings(indexed_dirs=[isolated_home / "Documentos"])
    settings.scan.include_hidden = True
    save_settings(app, settings)
    loaded = load_settings(app)
    assert loaded.indexed_dirs == [isolated_home / "Documentos"]
    assert loaded.scan.include_hidden is True


def test_invalid_settings_fall_back_to_defaults(isolated_home: Path) -> None:
    app = AppPaths.from_env()
    app.ensure()
    app.config_file.write_text("esto no es = = toml", encoding="utf-8")
    assert load_settings(app) == Settings()
