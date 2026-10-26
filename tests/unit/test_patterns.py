from __future__ import annotations

from datetime import datetime

import pytest

from organizador.learning.patterns import dominant_pattern, infer_pattern, render


def test_plan_example() -> None:
    pattern = infer_pattern("2024-05-10_informe_v2.pdf")
    assert pattern.template == "{fecha:AAAA-MM-DD}_{texto}_v{n}"
    assert pattern.case == "minúsculas"


@pytest.mark.parametrize(
    ("name", "template", "case"),
    [
        ("Cartola Banco 2024-03.pdf", "{texto} {fecha:AAAA-MM}", "Título"),
        ("acta_reunion_2024_05_10.md", "{texto}_{fecha:AAAA_MM_DD}", "minúsculas"),
        ("IMG_20240611_153001.jpg", "{texto}_{fecha:AAAAMMDD}_{n}", "MAYÚSCULAS"),
        (
            "Tarea 27-08-2026 Gestión de Proyectos.docx",
            "{texto} {fecha:DD-MM-AAAA} {texto}",
            "Título",
        ),
        ("informe final (1).docx", "{texto}", "minúsculas"),  # la marca de copia no cuenta
        ("hello-algo_codes_java.java", "{texto}", "minúsculas"),  # separadores mezclados
        ("image_000003_6cba5583858d1f0c9fed.png", "{texto}_{n}_{id}", "minúsculas"),
        ("Informe {borrador} V1.2.txt", "{texto} {{{texto}}} V{n}", "Oración"),
        ("写真_2024-01-02.jpg", "{texto}_{fecha:AAAA-MM-DD}", "—"),
    ],
)
def test_infer(name: str, template: str, case: str) -> None:
    pattern = infer_pattern(name)
    assert (pattern.template, pattern.case) == (template, case)


def test_dominant_pattern_needs_majority_and_information() -> None:
    names = ["2024-05-10_a_v1.pdf", "2024-06-10_b_v2.pdf", "2024-07-01_c_v1.pdf", "otro.pdf"]
    found = dominant_pattern(names)
    assert found is not None
    assert found.pattern.template == "{fecha:AAAA-MM-DD}_{texto}_v{n}"
    assert (found.support, found.total) == (3, 4)
    assert dominant_pattern(names[:2]) is None  # poco soporte
    assert dominant_pattern(["uno.pdf", "dos.pdf", "tres.pdf"]) is None  # solo {texto}
    assert dominant_pattern([*names[:3], "x.pdf", "y.pdf", "z.pdf", "w.pdf"]) is None


def test_render_follows_folder_pattern() -> None:
    pattern = infer_pattern("2024-05-10_informe_v2.pdf")
    when = datetime(2025, 1, 15)
    # Sin fecha en el nombre se usa la de modificación; el separador sale de la plantilla.
    assert render(pattern, "Presupuesto Casa V3.pdf", when) == "2025-01-15_presupuesto_casa_v3.pdf"
    assert render(pattern, "notas 2023-01-02 reunión v1.txt", when) == (
        "2023-01-02_notas_reunión_v1.txt"
    )
    assert render(pattern, "presupuesto.pdf", when) is None  # falta {n}


def test_render_case_and_unfillable_templates() -> None:
    when = datetime(2024, 3, 1)
    title = infer_pattern("Cartola Banco 2024-03.pdf")
    assert render(title, "estado de cuenta.pdf", when) == "Estado de Cuenta 2024-03.pdf"
    hashed = infer_pattern("image_000003_6cba5583858d1f0c9fed.png")
    assert render(hashed, "foto 1.png", when) is None
