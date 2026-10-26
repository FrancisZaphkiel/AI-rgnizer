from __future__ import annotations

from pathlib import Path

import pytest

from organizador.cli import main
from organizador.core.config import LearningSettings, ScanSettings
from organizador.learning.dataset import build_dataset
from organizador.learning.folders import FolderNames
from organizador.learning.review import find_misplaced
from organizador.scanner import Scanner
from organizador.storage import Database

pytestmark = pytest.mark.integration

SETTINGS = LearningSettings(min_examples=3, review_min_confidence=0.6)


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


@pytest.fixture
def docs(tmp_path: Path) -> Path:
    d = tmp_path / "Documentos"
    for i in range(15):
        _write(d / f"Banco/cartola_mensual_banco_{i}.pdf", f"b{i}")
        _write(d / f"Universidad/apuntes_calculo_clase_{i}.txt", f"u{i}")
    # Una cartola guardada por error entre los apuntes.
    _write(d / "Universidad/cartola_mensual_banco_99.pdf", "error")
    # Una receta en Varios, con una carpeta «Salud» casi vacía.
    for i in range(3):
        _write(d / f"Varios/nota_{i}.txt", f"v{i}")
    _write(d / "Varios/salud_receta.txt", "r")
    _write(d / "Salud/leeme.md", "s")
    return d


def test_finds_misplaced_file_and_ignores_well_placed(db: Database, docs: Path) -> None:
    Scanner(db, ScanSettings(), mounts=()).scan(docs)
    dataset = build_dataset(db, [docs], SETTINGS)
    report = find_misplaced(dataset, SETTINGS, FolderNames.from_index(db, [docs]))
    found = report.misplaced
    assert report.reviewed == len(dataset.examples)
    by_path = {m.path.name: m for m in found}
    assert "cartola_mensual_banco_99.pdf" in by_path
    assert by_path["cartola_mensual_banco_99.pdf"].target == str(docs / "Banco")
    assert not any(m.path.name.startswith("apuntes") for m in found)
    # Pocos falsos positivos: la gran mayoría de los 34 archivos está bien.
    assert len(found) <= 3


def test_folder_name_points_to_small_folder(db: Database, docs: Path) -> None:
    Scanner(db, ScanSettings(), mounts=()).scan(docs)
    folders = FolderNames.from_index(db, [docs])
    match = folders.match(["salud", "receta"])
    assert match is not None and match.folder == str(docs / "Salud")


def test_cli_review_moves_after_confirmation(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    docs = isolated_home / "Documentos"
    for i in range(12):
        _write(docs / f"Banco/cartola_mensual_banco_{i}.pdf", f"b{i}")
        _write(docs / f"Universidad/apuntes_calculo_clase_{i}.txt", f"u{i}")
    wrong = docs / "Universidad/cartola_mensual_banco_99.pdf"
    _write(wrong, "error")

    assert main(["review", str(docs)]) == 0
    out = capsys.readouterr().out
    assert "cartola_mensual_banco_99.pdf" in out
    assert wrong.exists()  # sin --move solo informa

    answers = iter(["s"])
    monkeypatch.setattr("builtins.input", lambda _p: next(answers, "n"))
    assert main(["review", "--no-scan", "--move", str(docs)]) == 0
    assert not wrong.exists()
    assert (docs / "Banco" / wrong.name).exists()


def test_suggest_says_when_already_in_place(
    isolated_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    docs = isolated_home / "Documentos"
    for i in range(10):
        _write(docs / f"Banco/cartola_{i}.pdf", f"b{i}")
        _write(docs / f"Fotos/IMG_{i}.jpg", f"f{i}")
    assert main(["train", str(docs)]) == 0
    capsys.readouterr()
    assert main(["suggest", str(docs / "Banco/cartola_3.pdf")]) == 0
    assert "ya está en su carpeta" in capsys.readouterr().out


def test_skips_bundles_generic_names_and_deliberate_copies(db: Database, tmp_path: Path) -> None:
    d = tmp_path / "Documentos"
    for i in range(15):
        _write(d / f"Banco/cartola_mensual_banco_{i}.pdf", f"b{i}")
        _write(d / f"Universidad/apuntes_calculo_clase_{i}.txt", f"u{i}")
    # Misma «cartola» en las dos carpetas: copia deliberada.
    _write(d / "Universidad/cartola_mensual_banco_1.pdf", "copia distinta")
    # Web exportada: anidada y con nombres genéricos repetidos.
    for i in range(4):
        _write(d / f"Universidad/web/pagina_{i}/cartola_mensual_banco.pdf", f"w{i}")
        _write(d / f"Universidad/res_{i}/image_00000{i}_ab90e1622444421e.pdf", f"i{i}")
    Scanner(db, ScanSettings(), mounts=()).scan(d)
    found = find_misplaced(build_dataset(db, [d], SETTINGS), SETTINGS).misplaced
    names = {m.path.name for m in found}
    assert "cartola_mensual_banco_1.pdf" not in names
    assert "cartola_mensual_banco.pdf" not in names
    assert not any(n.startswith("image_") for n in names)
