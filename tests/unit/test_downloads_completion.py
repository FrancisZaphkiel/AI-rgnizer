from __future__ import annotations

from pathlib import Path

import pytest

from organizador.downloads.completion import (
    Arrival,
    StabilityTracker,
    has_partial_sibling,
    is_temporary,
)


@pytest.mark.parametrize(
    ("name", "temporary"),
    [
        ("informe.pdf", False),
        ("...y ahora qué.mp3", False),
        ("informe.pdf.part", True),
        ("Unconfirmed 123456.crdownload", True),
        ("video.mkv.PART", True),
        (".goutputstream-ABC", True),
        (".~lock.hoja.ods#", True),
        ("notas.txt~", True),
    ],
)
def test_is_temporary(name: str, temporary: bool) -> None:
    assert is_temporary(Path(name)) is temporary


def test_firefox_placeholder_has_partial_sibling(tmp_path: Path) -> None:
    placeholder = tmp_path / "foto.jpg"
    placeholder.touch()
    assert not has_partial_sibling(placeholder)
    (tmp_path / "foto.jpg.part").write_bytes(b"x")
    assert has_partial_sibling(placeholder)


def test_arrival_ignores_directories_symlinks_and_temporaries(tmp_path: Path) -> None:
    (tmp_path / "carpeta").mkdir()
    (tmp_path / "real.txt").write_text("x")
    (tmp_path / "enlace.txt").symlink_to(tmp_path / "real.txt")
    (tmp_path / "a.part").write_text("x")
    assert Arrival.of(tmp_path / "carpeta") is None
    assert Arrival.of(tmp_path / "enlace.txt") is None
    assert Arrival.of(tmp_path / "a.part") is None
    assert Arrival.of(tmp_path / "no-existe") is None
    arrival = Arrival.of(tmp_path / "real.txt")
    assert arrival is not None and arrival.size == 1


def test_tracker_waits_for_stable_size(tmp_path: Path) -> None:
    path = tmp_path / "grande.iso"
    path.write_bytes(b"a")
    tracker = StabilityTracker(stable_seconds=3)
    tracker.notice(path, now=0)
    assert tracker.ready(now=2) == []

    with path.open("ab") as fh:  # sigue llegando
        fh.write(b"bb")
    assert tracker.ready(now=3) == []  # cambió: la espera vuelve a empezar
    assert tracker.ready(now=5) == []
    ready = tracker.ready(now=6)
    assert [a.path for a in ready] == [path] and ready[0].size == 3
    assert len(tracker) == 0


def test_tracker_holds_firefox_placeholder_until_part_disappears(tmp_path: Path) -> None:
    final, part = tmp_path / "doc.pdf", tmp_path / "doc.pdf.part"
    final.touch()
    part.write_bytes(b"parcial")
    tracker = StabilityTracker(stable_seconds=1)
    tracker.notice(final, now=0)
    tracker.notice(part, now=0)
    assert tracker.ready(now=10) == []

    part.replace(final)  # Firefox termina: renombra el .part sobre el marcador
    tracker.notice(final, now=11)
    assert [a.size for a in tracker.ready(now=12)] == [len(b"parcial")]


def test_tracker_forgets_files_that_vanish(tmp_path: Path) -> None:
    path = tmp_path / "x.txt"
    path.write_text("x")
    tracker = StabilityTracker(stable_seconds=1)
    tracker.notice(path, now=0)
    path.rename(tmp_path / "y.txt")
    assert tracker.ready(now=5) == []
    assert len(tracker) == 0
