from __future__ import annotations

from pathlib import Path

import pytest

from organizador.cli import main
from organizador.core.config import Settings, save_settings
from organizador.core.paths import AppPaths

pytestmark = pytest.mark.integration


@pytest.fixture
def organized(isolated_home: Path) -> Path:
    docs = isolated_home / "Documentos"
    for i in range(10):
        for folder, name in (("Banco", f"cartola_2024-{i + 1:02d}.pdf"), ("Fotos", f"IMG_{i}.jpg")):
            path = docs / folder / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(name.encode())
    return docs


def test_suggest_without_model_uses_type_destination(
    isolated_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    probe = isolated_home / "x.pdf"
    probe.write_bytes(b"%PDF-1.4")
    assert main(["suggest", str(probe)]) == 0
    captured = capsys.readouterr()
    assert "organizador train" in captured.err
    assert f"→   —  {isolated_home / 'Documents'}" in captured.out
    assert "destino por defecto" in captured.out


def test_train_suggest_teach_and_revert(
    isolated_home: Path, organized: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["train", str(organized)]) == 0
    out = capsys.readouterr().out
    assert "20 ejemplos en 2 carpetas" in out
    assert "Modelo 1 guardado" in out

    new = isolated_home / "Descargas/cartola_2025-01.pdf"
    new.parent.mkdir(parents=True)
    new.write_bytes(b"nueva")
    assert main(["suggest", str(new)]) == 0
    out = capsys.readouterr().out
    assert "→" in out and str(organized / "Banco") in out
    assert new.exists()  # sugerir nunca mueve

    assert main(["teach", str(new), str(organized / "Fotos")]) == 0
    assert "modelo 2" in capsys.readouterr().out
    assert new.exists()

    assert main(["model", "list"]) == 0
    listing = capsys.readouterr().out.splitlines()
    assert [line.split()[0] for line in listing] == ["1", "*"]

    assert main(["model", "revert", "1"]) == 0
    assert main(["model", "revert", "9"]) == 1


def test_teach_new_folder_waits_for_retraining(
    isolated_home: Path, organized: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["train", str(organized)]) == 0
    new_folder = organized / "Salud"
    new_folder.mkdir()
    probe = isolated_home / "receta.pdf"
    probe.write_bytes(b"r")
    capsys.readouterr()
    assert main(["teach", str(probe), str(new_folder)]) == 0
    assert "lo aprenderé tras 3 archivos" in capsys.readouterr().out
    assert main(["train", str(organized), "--no-scan"]) == 0
    assert "(1 correcciones)" in capsys.readouterr().out


def test_evaluate_prints_metrics(organized: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["evaluate", str(organized)]) == 0
    out = capsys.readouterr().out
    assert "top-1" in out and "ECE" in out


def test_train_needs_two_folders(isolated_home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    only = isolated_home / "Documentos/A"
    only.mkdir(parents=True)
    for i in range(6):
        (only / f"{i}.txt").write_text(str(i))
    assert main(["train", str(isolated_home / "Documentos")]) == 1
    assert "al menos dos carpetas" in capsys.readouterr().err


def _configure(docs: Path, **learning: object) -> None:
    settings = Settings(indexed_dirs=[docs])
    for key, value in learning.items():
        setattr(settings.learning, key, value)
    save_settings(AppPaths.from_env(), settings)


def test_config_rule_wins_over_learned(
    isolated_home: Path, organized: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _configure(organized, rules={"ext:pdf": "~/Papeles"})
    assert main(["train", str(organized)]) == 0
    probe = isolated_home / "Descargas/cartola_2025-01.pdf"
    probe.parent.mkdir()
    probe.write_bytes(b"c")
    capsys.readouterr()
    assert main(["suggest", str(probe)]) == 0
    out = capsys.readouterr().out
    assert f"→   —  {isolated_home / 'Papeles'}" in out
    assert "por tu regla en config.toml" in out


def test_mv_unclassified_learns_new_folder(
    isolated_home: Path, organized: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Sin clasificar → el usuario lo mueve con la aplicación → al tercero, reentrena."""
    _configure(organized)
    assert main(["train", str(organized)]) == 0
    downloads = isolated_home / "Descargas"
    downloads.mkdir()
    target = organized / "Salud"
    for i in range(3):
        receta = downloads / f"receta medica {i}.pdf"
        receta.write_bytes(f"r{i}".encode())
        capsys.readouterr()
        assert main(["mv", "-p", str(receta), str(target)]) == 0
        assert (target / receta.name).exists()
    assert "reentrenado (modelo 2)" in capsys.readouterr().out

    probe = downloads / "receta medica 9.pdf"
    probe.write_bytes(b"r9")
    assert main(["suggest", str(probe)]) == 0
    assert str(target) in capsys.readouterr().out


def test_mv_no_learn_records_nothing(
    isolated_home: Path, organized: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    src = isolated_home / "a.txt"
    src.write_text("a")
    assert main(["mv", "--no-learn", str(src), str(organized / "Banco")]) == 0
    assert "anotado" not in capsys.readouterr().out
