from __future__ import annotations

import re
from pathlib import Path

import pytest

from organizador.cli import main
from organizador.core.config import LearningSettings, Settings, WatchSettings, save_settings
from organizador.core.paths import AppPaths

pytestmark = pytest.mark.integration


def ids_by_name(out: str) -> dict[str, str]:
    return {Path(p).name: i for i, p in re.findall(r"^\[(\d+)\] (.+)$", out, re.MULTILINE)}


@pytest.fixture
def downloads(isolated_home: Path) -> Path:
    folder = isolated_home / "Descargas"
    folder.mkdir()
    docs = isolated_home / "Documentos"
    save_settings(
        AppPaths.from_env(),
        Settings(
            indexed_dirs=[docs],
            watched_dirs=[folder],
            watch=WatchSettings(stable_seconds=0.05),
            learning=LearningSettings(rules={"ext:pdf": str(docs / "Banco")}, type_destinations={}),
        ),
    )
    return folder


def test_watch_once_pending_accept_and_ignore(
    isolated_home: Path, downloads: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (downloads / "cartola.pdf").write_bytes(b"%PDF-1.4")
    (downloads / "raro.xyz").write_bytes(b"?")
    (downloads / "a medias.zip.part").write_bytes(b"...")

    assert main(["watch", "--once"]) == 0
    out = capsys.readouterr().out
    assert "[1]" in out and str(isolated_home / "Documentos/Banco") in out
    assert "sin clasificar" in out
    assert "a medias" not in out

    assert main(["watch", "--once"]) == 0  # nada nuevo: no se repite
    assert capsys.readouterr().out == ""

    assert main(["pending"]) == 0
    ids = ids_by_name(capsys.readouterr().out)
    assert sorted(ids) == ["cartola.pdf", "raro.xyz"]

    assert main(["accept", ids["cartola.pdf"]]) == 0
    assert (isolated_home / "Documentos/Banco/cartola.pdf").exists()
    assert "anotado" in capsys.readouterr().out  # registrado para aprender

    assert main(["accept", ids["cartola.pdf"]]) == 2
    assert "ya está resuelta" in capsys.readouterr().err

    assert main(["accept", ids["raro.xyz"]]) == 2  # sin destino sugerido ni --to
    assert main(["ignore", ids["raro.xyz"]]) == 0
    assert (downloads / "raro.xyz").exists()
    capsys.readouterr()
    assert main(["pending"]) == 0
    assert "No hay sugerencias pendientes" in capsys.readouterr().out

    assert main(["history"]) == 0
    assert "cartola.pdf" in capsys.readouterr().out


def test_accept_to_other_folder(
    isolated_home: Path, downloads: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (downloads / "cartola.pdf").write_bytes(b"%PDF-1.4")
    assert main(["watch", "--once"]) == 0
    capsys.readouterr()
    other = isolated_home / "Documentos/Trabajo"
    assert main(["accept", "1", "--to", str(other)]) == 0
    assert (other / "cartola.pdf").exists()


def test_watch_auto_moves_by_user_rule(isolated_home: Path, downloads: Path) -> None:
    (isolated_home / "Documentos/Banco").mkdir(parents=True)
    (downloads / "cartola.pdf").write_bytes(b"%PDF-1.4")
    assert main(["watch", "--once", "--auto"]) == 0
    assert (isolated_home / "Documentos/Banco/cartola.pdf").exists()
    assert not (downloads / "cartola.pdf").exists()


def test_accept_unknown_id(downloads: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["accept", "99"]) == 2
    assert "No hay ninguna sugerencia 99" in capsys.readouterr().err
