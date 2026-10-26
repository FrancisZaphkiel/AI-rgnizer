from __future__ import annotations

from airgnizer.learning.folders import FolderNames, folder_words


def test_folder_words_ignore_short_and_numeric() -> None:
    assert folder_words("Salud 2024") == {"salud"}
    assert folder_words("Ingeníeria de Requerimientos") == {"ingenieria", "requerimientos"}
    assert folder_words("doc") == frozenset()


def test_all_folder_words_must_appear() -> None:
    names = FolderNames(["/d/Salud", "/d/Gestión de proyectos"])
    assert names.match(["salud", "receta"]) is not None
    assert names.match(["proyectos", "informe"]) is None
    match = names.match(["gestion", "proyectos", "acta"])
    assert match is not None and match.folder == "/d/Gestión de proyectos"


def test_most_specific_wins_and_ties_are_ambiguous() -> None:
    names = FolderNames(["/d/Proyectos", "/d/Proyectos Salud", "/x/Salud", "/y/Salud"])
    best = names.match(["proyectos", "salud"])
    assert best is not None and best.folder == "/d/Proyectos Salud"
    assert names.match(["salud"]) is None  # dos carpetas «Salud» distintas
