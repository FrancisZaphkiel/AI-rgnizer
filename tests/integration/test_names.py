from __future__ import annotations

import os
from pathlib import Path

import pytest

from airgnizer.cli import main
from airgnizer.core.config import ScanSettings
from airgnizer.duplicates.names import NameReport, SimilarNameFinder
from airgnizer.scanner import Scanner
from airgnizer.storage import Database

pytestmark = pytest.mark.integration


def _write(path: Path, content: bytes | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(os.urandom(64) if content is None else content)
    return path


def _find(db: Database, root: Path, **kw: int) -> NameReport:
    Scanner(db, ScanSettings(), mounts=()).scan(root)
    return SimilarNameFinder(db, **kw).find([root])


def _names(report: NameReport) -> list[set[str]]:
    return [{f.path.name for f in g.files} for g in report.groups]


def test_similar_names_grouped_across_folders(db: Database, tmp_path: Path) -> None:
    root = tmp_path / "u"
    _write(root / "Documentos/Informe Final.pdf")
    _write(root / "Descargas/informe_final (1).pdf")
    _write(root / "Descargas/Robin [ Live Wallpaper ]-DSEgT1etldQ.mp4")
    _write(root / "Vídeos/Robin [ Live Wallpaper ]-pN_dYXS4U0w.mp4")
    _write(root / "Otros/presupuesto.pdf")

    report = _find(db, root)

    assert sorted(_names(report), key=sorted) == sorted(
        [
            {"Informe Final.pdf", "informe_final (1).pdf"},
            {
                "Robin [ Live Wallpaper ]-DSEgT1etldQ.mp4",
                "Robin [ Live Wallpaper ]-pN_dYXS4U0w.mp4",
            },
        ],
        key=sorted,
    )
    assert all(g.identical_names for g in report.groups)


def test_different_extension_series_and_short_names_ignored(db: Database, tmp_path: Path) -> None:
    root = tmp_path / "u"
    _write(root / "informe final.pdf")
    _write(root / "informe final.odt")  # otra extensión
    _write(root / "screen-20241106-232527.mp4")
    _write(root / "screen-20241106-232716.mp4")  # serie
    _write(root / "a.txt")
    _write(root / "b.txt")  # nombres demasiado cortos
    assert _find(db, root).groups == []


def test_exact_copies_and_hardlinks_are_left_to_dupes(db: Database, tmp_path: Path) -> None:
    root = tmp_path / "u"
    original = _write(root / "foto playa.jpg", b"x" * 100)
    os.link(original, root / "foto playa (1).jpg")
    assert _find(db, root).groups == []


def test_score_threshold(db: Database, tmp_path: Path) -> None:
    root = tmp_path / "u"
    _write(root / "Horse Race - Majora's Mask OST.mp3")
    _write(root / "Horse Race Goal - Majora's Mask OST.mp3")
    assert len(_find(db, root, min_score=90).groups) == 1
    assert SimilarNameFinder(db, min_score=99).find([root]).groups == []


def test_cli_names_only_reports(isolated_home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    a = _write(isolated_home / "Documentos/Informe Final.pdf")
    b = _write(isolated_home / "Descargas/informe final (1).pdf")
    assert main(["names", str(isolated_home)]) == 0
    out = capsys.readouterr().out
    assert "1 grupos de nombres parecidos" in out
    assert "airgnizer trash ARCHIVO" in out
    assert a.exists() and b.exists()
