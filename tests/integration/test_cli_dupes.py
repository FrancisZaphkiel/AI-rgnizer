from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from airgnizer.cli import human_size, main

pytestmark = pytest.mark.integration


@pytest.fixture
def dupes(isolated_home: Path) -> tuple[Path, Path, Path]:
    data = os.urandom(5000)
    keep = isolated_home / "Documentos/a.pdf"
    copy1 = isolated_home / "Descargas/a.pdf"
    copy2 = isolated_home / "Descargas/a (1).pdf"
    for path in (keep, copy1, copy2):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return keep, copy1, copy2


def _answers(monkeypatch: pytest.MonkeyPatch, *answers: str) -> None:
    pending = iter(answers)

    def fake_input(_prompt: str) -> str:
        try:
            return next(pending)
        except StopIteration:
            raise EOFError from None

    monkeypatch.setattr("builtins.input", fake_input)


def test_human_size() -> None:
    assert human_size(0) == "0 B"
    assert human_size(1023) == "1023 B"
    assert human_size(1536) == "1.5 KiB"
    assert human_size(5 * 1024**4) == "5120.0 GiB"


def test_dupes_lists_groups_without_touching_files(
    isolated_home: Path, dupes: tuple[Path, ...], capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["dupes", str(isolated_home)]) == 0
    out = capsys.readouterr().out
    assert "1 grupos de duplicados exactos" in out
    assert re.search(r"\[\d\] conservar\s+\S*Documentos/a\.pdf", out)
    assert all(p.exists() for p in dupes)


def test_dupes_requires_a_root(isolated_home: Path) -> None:
    assert main(["dupes"]) == 2


@pytest.mark.parametrize("command", ["dupes", "scan"])
def test_missing_root_reports_error_without_traceback(
    isolated_home: Path, command: str, capsys: pytest.CaptureFixture[str]
) -> None:
    (isolated_home / "Música").mkdir()
    missing = isolated_home / "home/francisco/Música"
    assert main([command, str(isolated_home / "Música"), str(missing)]) == 2
    err = capsys.readouterr().err
    assert f"no es una carpeta: {missing}" in err
    assert "Traceback" not in err


def test_dupes_trash_declined_keeps_everything(
    isolated_home: Path, dupes: tuple[Path, ...], monkeypatch: pytest.MonkeyPatch
) -> None:
    _answers(monkeypatch, "n")
    assert main(["dupes", str(isolated_home), "--trash"]) == 0
    assert all(p.exists() for p in dupes)


def test_dupes_trash_with_other_keeper_then_undo(
    isolated_home: Path,
    dupes: tuple[Path, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    keep, copy1, copy2 = dupes
    # Los archivos se listan ordenados por ruta: [1] Descargas/a (1).pdf, [2] Descargas/a.pdf …
    _answers(monkeypatch, "x", "2", "s")
    assert main(["dupes", str(isolated_home), "--trash"]) == 0
    out = capsys.readouterr().out
    assert "Respuesta no válida" in out
    assert copy1.exists()
    assert not keep.exists() and not copy2.exists()

    ids = re.findall(r"^\s+([0-9a-f]{8})\s+.* → papelera$", out, flags=re.MULTILINE)
    assert len(ids) == 2
    for action_id in ids:
        assert main(["undo", action_id]) == 0
    assert keep.exists() and copy2.exists()


def test_dupes_trash_yes_without_prompt(
    isolated_home: Path, dupes: tuple[Path, Path, Path]
) -> None:
    keep, copy1, copy2 = dupes
    assert main(["dupes", str(isolated_home), "--trash", "--yes"]) == 0
    assert keep.exists()
    assert not copy1.exists() and not copy2.exists()


def test_dupes_no_scan_uses_existing_index(
    isolated_home: Path, dupes: tuple[Path, ...], capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["dupes", str(isolated_home), "--no-scan"]) == 0
    assert "0 grupos" in capsys.readouterr().out  # nada indexado todavía
    assert main(["scan", str(isolated_home)]) == 0
    assert main(["dupes", str(isolated_home), "--no-scan", "--min-size", "1"]) == 0
    assert "1 grupos" in capsys.readouterr().out
