from __future__ import annotations

import time
from pathlib import Path

from airgnizer.core.paths import FolderSet
from airgnizer.learning.folders import FolderNames


def test_folder_set_matches_folders_and_their_contents_only() -> None:
    skipped = FolderSet([Path("/home/ana/Descargas"), "/home/ana/repos/"])
    assert "/home/ana/Descargas" in skipped
    assert "/home/ana/Descargas/sub" in skipped
    assert Path("/home/ana/repos/web/static") in skipped
    assert "/home/ana/Descargas2" not in skipped  # prefijo de texto, no de carpeta
    assert "/home/ana" not in skipped
    assert "/x" in FolderSet(["/"])
    assert not FolderSet([])


def test_folder_names_with_a_big_index_is_fast() -> None:
    """16 000 carpetas y 40 exclusiones: antes ~100 s (el servicio no llegaba a arrancar)."""
    folders = [f"/home/ana/Documentos/p{i // 100}/carpeta_{i}" for i in range(16_000)]
    skipped = FolderSet(f"/home/ana/Documentos/p{i}" for i in range(0, 160, 4))
    start = time.perf_counter()
    names = FolderNames(f for f in folders if f not in skipped)
    assert time.perf_counter() - start < 2
    assert len(names) == 12_000
