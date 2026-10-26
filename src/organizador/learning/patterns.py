"""Patrones de nombres: cómo nombra el usuario los archivos de cada carpeta.

``2024-05-10_informe_v2.pdf`` → ``{fecha:AAAA-MM-DD}_{texto}_v{n}`` (en minúsculas). Si la
mayoría de los archivos de una carpeta siguen una plantilla, se puede proponer el nombre
que tendría un archivo nuevo al moverlo allí (``render``). Nunca se renombra solo: es una
sugerencia que el usuario aplica con «organizador rename».

Piezas de una plantilla:

- ``{fecha:FORMATO}``: una fecha del nombre (formatos de ``features.name``, con el
  separador real: ``AAAA_MM_DD``);
- ``{texto}``: palabras seguidas, unidas siempre por el mismo separador;
- ``{n}``: un número (``v{n}`` cuando es una versión);
- ``{id}``: un hash o identificador (no se puede generar: ``render`` no lo rellena);
- el resto (``_``, `` - ``, ``(``) se conserva literal.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime

from organizador.duplicates.keep import strip_copy_marker
from organizador.features.name import VERSION, find_date, split_name, strip_video_id

MIN_SUPPORT = 3
MIN_FRACTION = 0.5
# Separadores que pueden unir las palabras de un mismo {texto}.
_TEXT_JOINERS = frozenset(" _-.")
_CONNECTOR_MAX = 3
_RUNS = re.compile(
    # Hash o identificador: 8+ caracteres hexadecimales que mezclan letras y dígitos.
    r"(?P<id>(?<![0-9A-Za-z])(?=[0-9a-fA-F]{8})(?=[0-9a-fA-F]*\d)(?=[0-9a-fA-F]*[a-fA-F])"
    r"[0-9a-fA-F]+(?![0-9A-Za-z]))"
    r"|(?P<texto>[^\W\d_]+)|(?P<n>\d+)|(?P<lit>[\W_]+)"
)


@dataclass(frozen=True, slots=True)
class _Piece:
    kind: str  # fecha | texto | n | version | id | lit
    text: str  # texto original del archivo
    shape: str = ""  # formato de la fecha o prefijo de la versión («v», «V», «ver_»)


@dataclass(frozen=True, slots=True)
class NamePattern:
    template: str
    case: str  # minúsculas | MAYÚSCULAS | Título | Oración | mixto | —
    text_separator: str | None

    def __str__(self) -> str:
        return self.template if self.case == "—" else f"{self.template} ({self.case})"


def _date_shape(fmt: str, matched: str) -> str:
    """«AAAA-MM-DD» con el separador real del archivo («2024_05_10» → «AAAA_MM_DD»)."""
    seps = [c for c in matched if not c.isdigit()]
    out, i = [], 0
    for part in re.findall(r"AAAA|MM|DD|-", fmt):
        if part == "-":
            out.append(seps[i] if i < len(seps) else "-")
            i += 1
        else:
            out.append(part)
    return "".join(out)


def _pieces(stem: str) -> list[_Piece]:
    spans: list[tuple[int, int, _Piece]] = []
    if (found := find_date(stem)) is not None:
        date, (start, end) = found
        spans.append(
            (
                start,
                end,
                _Piece("fecha", stem[start:end], _date_shape(date.format, stem[start:end])),
            )
        )
    for match in VERSION.finditer(stem):
        start, end = match.span()
        if any(s < end and start < e for s, e, _ in spans):
            continue
        prefix = stem[start : match.start("n")]
        spans.append((start, end, _Piece("version", match["n"], prefix)))
        break
    spans.sort()
    pieces: list[_Piece] = []
    cursor = 0
    for start, end, piece in [*spans, (len(stem), len(stem), None)]:
        for run in _RUNS.finditer(stem[cursor:start]):
            kind = run.lastgroup or "lit"
            pieces.append(_Piece(kind, run.group()))
        if piece is not None:
            pieces.append(piece)
        cursor = end
    return _merge_text(pieces)


def _merge_text(pieces: list[_Piece]) -> list[_Piece]:
    """«informe final» (texto, « », texto) → un solo {texto}; guarda el separador más común."""
    out: list[_Piece] = []
    i = 0
    while i < len(pieces):
        piece = pieces[i]
        if piece.kind == "texto":
            text, seps = piece.text, Counter[str]()
            while (
                i + 2 < len(pieces)
                and pieces[i + 1].kind == "lit"
                and pieces[i + 1].text in _TEXT_JOINERS
                and pieces[i + 2].kind == "texto"
            ):
                seps[pieces[i + 1].text] += 1
                text += pieces[i + 1].text + pieces[i + 2].text
                i += 2
            sep = seps.most_common(1)[0][0] if seps else ""
            out.append(_Piece("texto", text, sep))
        else:
            out.append(piece)
        i += 1
    return out


def _case(words: Sequence[str]) -> str:
    letters = [w for w in words if any(c.isalpha() for c in w)]
    if not letters or not any(c.isupper() or c.islower() for w in letters for c in w):
        return "—"  # sin texto, o alfabetos sin mayúsculas
    if all(w == w.lower() for w in letters):
        return "minúsculas"
    if all(w == w.upper() for w in letters):
        return "MAYÚSCULAS"
    # «Gestión de Proyectos»: los conectores cortos en minúscula no rompen el «Título».
    capitalised = [w for w in letters if not (len(w) <= _CONNECTOR_MAX and w == w.lower())]
    if capitalised and all(w[:1].isupper() and w[1:] == w[1:].lower() for w in capitalised):
        return "Título" if len(letters) > 1 else "Oración"
    first, rest = letters[0], letters[1:]
    if first[:1].isupper() and first[1:] == first[1:].lower() and all(w == w.lower() for w in rest):
        return "Oración"
    return "mixto"


def _stem(name: str) -> str:
    stem, _ = split_name(name)
    return strip_copy_marker(strip_video_id(stem)).strip()


def infer_pattern(name: str) -> NamePattern:
    pieces = _pieces(_stem(name))
    parts: list[str] = []
    words: list[str] = []
    separator: str | None = None
    for piece in pieces:
        match piece.kind:
            case "fecha":
                parts.append(f"{{fecha:{piece.shape}}}")
            case "version":
                parts.append(piece.shape.replace("{", "{{").replace("}", "}}") + "{n}")
            case "n":
                parts.append("{n}")
            case "id":
                parts.append("{id}")
            case "texto":
                parts.append("{texto}")
                words.extend(re.split(r"[ _.\-]", piece.text))
                separator = separator or piece.shape or None
            case _:
                parts.append(piece.text.replace("{", "{{").replace("}", "}}"))
    return NamePattern("".join(parts), _case(words), separator)


# --- patrones por carpeta ----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FolderPattern:
    pattern: NamePattern
    support: int
    total: int
    examples: tuple[str, ...]

    @property
    def fraction(self) -> float:
        return self.support / self.total if self.total else 0.0


def folder_patterns(names: Iterable[str]) -> list[FolderPattern]:
    """Plantillas de una carpeta, de más a menos frecuente (el *case* va aparte)."""
    by_template: dict[str, list[tuple[str, NamePattern]]] = defaultdict(list)
    for name in names:
        pattern = infer_pattern(name)
        by_template[pattern.template].append((name, pattern))
    total = sum(len(v) for v in by_template.values())
    out: list[FolderPattern] = []
    for template, items in by_template.items():
        case = Counter(p.case for _, p in items).most_common(1)[0][0]
        seps = Counter(p.text_separator for _, p in items if p.text_separator)
        sep = seps.most_common(1)[0][0] if seps else None
        examples = tuple(sorted(n for n, _ in items)[:3])
        out.append(FolderPattern(NamePattern(template, case, sep), len(items), total, examples))
    out.sort(key=lambda fp: (-fp.support, fp.pattern.template))
    return out


def dominant_pattern(
    names: Iterable[str], *, min_support: int = MIN_SUPPORT, min_fraction: float = MIN_FRACTION
) -> FolderPattern | None:
    """La plantilla que siguen la mayoría de los archivos, si tiene información útil."""
    patterns = folder_patterns(names)
    if not patterns:
        return None
    best = patterns[0]
    useful = best.pattern.template not in {"{texto}", "{n}", ""}
    if useful and best.support >= min_support and best.fraction >= min_fraction:
        return best
    return None


# --- aplicar un patrón a un archivo nuevo ------------------------------------------------


def _apply_case(text: str, case: str) -> str:
    match case:
        case "minúsculas":
            return text.lower()
        case "MAYÚSCULAS":
            return text.upper()
        case "Título":
            return " ".join(
                w.lower() if i > 0 and len(w) <= _CONNECTOR_MAX else w[:1].upper() + w[1:].lower()
                for i, w in enumerate(text.split(" "))
            )
        case "Oración":
            return text[:1].upper() + text[1:].lower()
        case _:
            return text


def _format_date(shape: str, when: datetime) -> str:
    values = {"AAAA": f"{when.year:04d}", "MM": f"{when.month:02d}", "DD": f"{when.day:02d}"}
    return re.sub(r"AAAA|MM|DD", lambda m: values[m.group()], shape)


def _template_separator(template: str) -> str:
    """Separador para unir palabras cuando la carpeta solo tiene textos de una palabra."""
    literals = re.sub(r"\{[^}]*\}", "", template)
    used = {c for c in literals if c in _TEXT_JOINERS}
    return used.pop() if len(used) == 1 else " "


def render(pattern: NamePattern, name: str, modified: datetime) -> str | None:
    """Nombre de ``name`` siguiendo ``pattern``; ``None`` si faltan datos para rellenarlo.

    La fecha sale del propio nombre o, si no tiene, de la fecha de modificación; el
    texto, de las palabras del nombre; ``{n}``, de su versión o sus números.
    """
    _, extension = split_name(name)
    pieces = _pieces(_stem(name))
    texts = [p.text for p in pieces if p.kind == "texto"]
    numbers = [p.text for p in pieces if p.kind == "version"] + [
        p.text for p in pieces if p.kind == "n"
    ]
    date_piece = next((p for p in pieces if p.kind == "fecha"), None)
    when = modified
    if date_piece is not None and (found := find_date(date_piece.text)) is not None:
        d = found[0]
        when = datetime(d.year, d.month, d.day or 1)

    template = pattern.template
    if "{id}" in template or template.count("{texto}") > 1 or (not texts and "{texto}" in template):
        return None
    joined = " ".join(re.split(r"[ _.\-]+", " ".join(texts))).strip()
    sep = pattern.text_separator or _template_separator(template)
    text = _apply_case(joined, pattern.case).replace(" ", sep)

    out: list[str] = []
    pos = 0
    for token in re.finditer(r"\{fecha:(?P<shape>[^}]+)\}|\{texto\}|\{n\}|\{\{|\}\}", template):
        out.append(template[pos : token.start()])
        value = token.group()
        if value.startswith("{fecha:"):
            out.append(_format_date(token["shape"], when))
        elif value == "{texto}":
            out.append(text)
        elif value == "{n}":
            if not numbers:
                return None
            out.append(numbers.pop(0))
        else:
            out.append(value[0])
        pos = token.end()
    out.append(template[pos:])
    result = "".join(out)
    return f"{result}.{extension}" if extension else result
