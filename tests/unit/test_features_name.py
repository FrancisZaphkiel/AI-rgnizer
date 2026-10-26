from __future__ import annotations

import pytest

from airgnizer.features.name import analyse_name, fold, split_name, strip_video_id


@pytest.mark.parametrize(
    ("name", "stem", "ext"),
    [
        ("informe.PDF", "informe", "pdf"),
        ("respaldo.tar.gz", "respaldo", "tar.gz"),
        (".bashrc", ".bashrc", ""),
        ("sin_extension", "sin_extension", ""),
        ("v1.0.final.docx", "v1.0.final", "docx"),
    ],
)
def test_split_name(name: str, stem: str, ext: str) -> None:
    assert split_name(name) == (stem, ext)


def test_fold_removes_case_and_accents() -> None:
    assert fold("Ingeníeria CÓDIGO Ñandú") == "ingenieria codigo nandu"


def test_fold_keeps_marks_of_other_scripts() -> None:
    assert fold("ゼンレスゾーンゼロ") == "ゼンレスゾーンゼロ"
    assert fold("Ёлка") == "ёлка"  # cirílico: la diéresis es parte de la letra


@pytest.mark.parametrize(
    ("name", "words"),
    [
        ("CartolaBancoEstado.pdf", ("cartola", "banco", "estado")),
        ("01 A PS Calidad de Software.pdf", ("01", "ps", "calidad", "software")),
        ("foto (1).jpg", ("foto",)),
        ("Copia de contrato.odt", ("contrato",)),
        ("id 123456789.pdf", ("id",)),  # números largos: identificadores, no palabras
        ("Música_Clásica.flac", ("musica", "clasica")),
        ("... Baby One More Time-91Niv2q4gvc.mp3", ("baby", "one", "more", "time")),
        ("ゼンレスゾーンゼロ.mp4", ("ゼンレスゾーンゼロ",)),
    ],
)
def test_words(name: str, words: tuple[str, ...]) -> None:
    assert analyse_name(name).words == words


@pytest.mark.parametrize(
    ("name", "fmt", "year", "month", "day"),
    [
        ("2024-05-10_informe.pdf", "AAAA-MM-DD", 2024, 5, 10),
        ("informe 10-05-2024.pdf", "DD-MM-AAAA", 2024, 5, 10),
        ("IMG_20240612_153012.jpg", "AAAAMMDD", 2024, 6, 12),
        ("cartola_2024-05.pdf", "AAAA-MM", 2024, 5, None),
        ("CartolaBancoEstado_202405.pdf", "AAAAMM", 2024, 5, None),
    ],
)
def test_dates(name: str, fmt: str, year: int, month: int, day: int | None) -> None:
    info = analyse_name(name)
    assert info.date is not None
    assert (info.date.format, info.date.year, info.date.month, info.date.day) == (
        fmt,
        year,
        month,
        day,
    )
    assert str(year) not in info.words  # la fecha no se repite como palabra


@pytest.mark.parametrize(
    "name",
    ["1716978858379.png", "pedido 2024-13-01.pdf", "factura 20241341.pdf", "copia 1999.txt"],
)
def test_no_false_dates(name: str) -> None:
    assert analyse_name(name).date is None


@pytest.mark.parametrize(
    ("name", "version"),
    [
        ("informe_v2.pdf", "2"),
        ("Calidad de Software V1.0.pdf", "1.0"),
        ("plan version 3.docx", "3"),
        ("plan-ver2.docx", "2"),
        ("nivel 2.pdf", None),
        ("ovni5.pdf", None),
    ],
)
def test_versions(name: str, version: str | None) -> None:
    assert analyse_name(name).version == version


def test_copy_marker_and_digits() -> None:
    assert analyse_name("foto (1).jpg").copy_marker
    assert not analyse_name("foto.jpg").copy_marker
    assert analyse_name("foto2.jpg").has_digits
    assert not analyse_name("foto.jpg").has_digits


@pytest.mark.parametrize(
    ("stem", "expected"),
    [
        ("Robin-DSEgT1etldQ", "Robin"),
        ("Shenhe [hP5WROKZ_mU]", "Shenhe"),
        ("Metal Gear Rising-Revengeance", "Metal Gear Rising-Revengeance"),
        ("sin id", "sin id"),
    ],
)
def test_strip_video_id(stem: str, expected: str) -> None:
    assert strip_video_id(stem) == expected
