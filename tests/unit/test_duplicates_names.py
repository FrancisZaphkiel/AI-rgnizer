from __future__ import annotations

import pytest

from airgnizer.duplicates.keep import strip_copy_marker
from airgnizer.duplicates.names import is_series, normalize


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Informe_Final.PDF", "informe final"),
        ("informe final (1).pdf", "informe final"),
        ("Copia de informe-final.pdf", "informe final"),
        ("Robin [ Live Wallpaper ]-DSEgT1etldQ.mp4", "robin [ live wallpaper ]"),
        ("Shenhe, What are you doing？ 😳 [hP5WROKZ_mU].mp4", "shenhe, what are you doing? 😳"),
        ("Mavuika Assadão DX! 😳-DvBdJCjeY2o.mp4", "mavuika assadão dx! 😳"),
        # Palabras de 11 letras al final no son IDs de vídeo.
        ("Metal Gear Rising-Revengeance.mp3", "metal gear rising revengeance"),
        ("informe-documentos.pdf", "informe documentos"),
        (".bashrc", "bashrc"),
    ],
)
def test_normalize(name: str, expected: str) -> None:
    assert normalize(name) == expected


def test_strip_copy_marker() -> None:
    assert strip_copy_marker("foto - copia (2)") == "foto"
    assert strip_copy_marker("Copy of foto") == "foto"
    assert strip_copy_marker("fotocopia") == "fotocopia"


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("screen 20241106 232527", "screen 20241106 232716"),
        ("zzz mommies shake part 3", "zzz mommies shake part 4"),
        ("prueba 2 tec seg", "prueba1 tec seg"),
        ("informe v1", "informe v2"),
    ],
)
def test_series_are_not_similar(a: str, b: str) -> None:
    assert is_series(a, b)


def test_same_name_or_different_words_are_not_series() -> None:
    assert not is_series("informe", "informe")
    assert not is_series("horse race", "horse race goal")
