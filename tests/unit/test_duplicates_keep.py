from __future__ import annotations

from pathlib import Path

import pytest

from airgnizer.duplicates import DuplicateFile, DuplicateGroup, KeepPolicy
from airgnizer.duplicates.keep import has_copy_marker

HOME = Path("/home/u")
POLICY = KeepPolicy(organized_dirs=[HOME / "Documentos"], watched_dirs=[HOME / "Descargas"])


def _file(
    path: str,
    *,
    mtime: int = 0,
    birth: int | None = None,
    inode: int | None = None,
    nlink: int = 1,
    exclusive: int = 100,
) -> DuplicateFile:
    return DuplicateFile(
        path=Path(path),
        size=100,
        mtime_ns=mtime,
        device=1,
        inode=inode if inode is not None else hash(path) & 0xFFFF,
        nlink=nlink,
        birth_ns=birth,
        exclusive_bytes=exclusive,
    )


def _group(*files: DuplicateFile) -> DuplicateGroup:
    choice = POLICY.choose(files)
    return DuplicateGroup("h", 100, files, choice.keep, choice.reason)


@pytest.mark.parametrize(
    "name",
    [
        "foto (1).jpg",
        "foto(2).jpg",
        "foto - copia.jpg",
        "foto - Copia (3).jpg",
        "foto_copy.png",
        "foto copy 2.png",
        "Copia de foto.jpg",
        "Copy of foto.jpg",
        "informe copia",
    ],
)
def test_copy_markers_detected(name: str) -> None:
    assert has_copy_marker(name)


@pytest.mark.parametrize(
    "name", ["foto.jpg", "fotocopia.pdf", "copia.txt", "2024 (resumen).pdf", "copyright.txt"]
)
def test_original_names_not_flagged(name: str) -> None:
    assert not has_copy_marker(name)


def test_prefers_organized_folder_over_downloads() -> None:
    choice = POLICY.choose(
        [
            _file("/home/u/Descargas/a.pdf", mtime=1),
            _file("/home/u/Documentos/Banco/a.pdf", mtime=9),
        ]
    )
    assert choice.keep.path == Path("/home/u/Documentos/Banco/a.pdf")
    assert choice.reason == "está en una carpeta organizada"


def test_prefers_outside_watched_when_none_organized() -> None:
    choice = POLICY.choose([_file("/home/u/Descargas/a.pdf"), _file("/home/u/Otros/a.pdf")])
    assert choice.keep.path == Path("/home/u/Otros/a.pdf")
    assert choice.reason == "está fuera de las carpetas vigiladas"


def test_prefers_name_without_copy_marker_then_oldest() -> None:
    choice = POLICY.choose(
        [_file("/home/u/Documentos/a (1).pdf", mtime=1), _file("/home/u/Documentos/a.pdf", mtime=5)]
    )
    assert choice.keep.path.name == "a.pdf"

    choice = POLICY.choose(
        [_file("/home/u/Documentos/x/a.pdf", mtime=5), _file("/home/u/Documentos/y/a.pdf", mtime=2)]
    )
    assert choice.keep.path == Path("/home/u/Documentos/y/a.pdf")
    assert choice.reason == "se creó antes que las demás"


def test_birth_time_beats_preserved_mtime() -> None:
    # Copia con la mtime conservada (Dolphin, cp -p) y ruta más corta: gana el original.
    original = _file("/home/u/Música/Techno Syndrome.mp3", mtime=10, birth=10)
    copy = _file("/home/u/Doc/musica.mp3", mtime=10, birth=99)
    choice = POLICY.choose([copy, original])
    assert choice.keep == original
    assert choice.reason == "se creó antes que las demás"


def test_without_birth_time_falls_back_to_mtime() -> None:
    old = _file("/home/u/Otros/largo/a.pdf", mtime=1)
    new = _file("/home/u/Otros/a.pdf", mtime=5)
    assert POLICY.choose([new, old]).keep == old


def test_same_birth_time_then_mtime_decides() -> None:
    a = _file("/home/u/Otros/largo/a.pdf", mtime=1, birth=7)
    b = _file("/home/u/Otros/a.pdf", mtime=5, birth=7)
    choice = POLICY.choose([b, a])
    assert choice.keep == a
    assert choice.reason == "es la modificada hace más tiempo"


def test_choice_is_independent_of_input_order() -> None:
    files = [_file(f"/home/u/Otros/{n}.pdf") for n in "cab"]
    assert POLICY.choose(files).keep == POLICY.choose(files[::-1]).keep


def test_user_choice_overrides_recommendation() -> None:
    a, b = _file("/home/u/Documentos/a.pdf"), _file("/home/u/Descargas/a.pdf")
    group = _group(a, b).with_keep(b)
    assert group.keep == b
    assert group.redundant == (a,)
    with pytest.raises(ValueError, match="no pertenece"):
        group.with_keep(_file("/otro"))


def test_hardlinks_of_keep_are_not_redundant_nor_recoverable() -> None:
    keep = _file("/home/u/Documentos/a.pdf", inode=1, nlink=2)
    link = _file("/home/u/Otros/a.pdf", inode=1, nlink=2)
    copy = _file("/home/u/Descargas/a.pdf", inode=2)
    group = _group(keep, link, copy)
    assert group.keep == keep
    assert group.hardlinks_of_keep == (link,)
    assert group.redundant == (copy,)
    assert group.recoverable_bytes == 100


def test_inode_with_links_outside_group_frees_nothing() -> None:
    keep = _file("/home/u/Documentos/a.pdf", inode=1)
    # Dos enlaces en el grupo, pero el inode tiene tres: el tercero sigue ocupando el espacio.
    l1 = _file("/home/u/Descargas/a.pdf", inode=2, nlink=3)
    l2 = _file("/home/u/Descargas/b.pdf", inode=2, nlink=3)
    assert _group(keep, l1, l2).recoverable_bytes == 0
    # Con todos sus enlaces dentro del grupo se libera una sola vez.
    l1, l2 = (_file(f.path.as_posix(), inode=2, nlink=2) for f in (l1, l2))
    assert _group(keep, l1, l2).recoverable_bytes == 100


def test_reflinked_copy_frees_nothing() -> None:
    keep = _file("/home/u/Documentos/a.pdf", inode=1, exclusive=0)
    reflink = _file("/home/u/Descargas/a.pdf", inode=2, exclusive=0)
    group = _group(keep, reflink)
    assert group.redundant == (reflink,)
    assert group.recoverable_bytes == 0
