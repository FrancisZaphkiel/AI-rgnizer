from __future__ import annotations

import os
from pathlib import Path

import pytest
from mutagen.easyid3 import EasyID3
from PIL import Image
from sqlalchemy import select

from airgnizer.core.config import ScanSettings
from airgnizer.features import FileFeatures, extract_features
from airgnizer.features import store as store_module
from airgnizer.features.metadata import extract_metadata, mime_type, origin_domain
from airgnizer.features.store import FeatureStats, indexed_features
from airgnizer.scanner import Scanner
from airgnizer.storage import Database
from airgnizer.storage.models import File

pytestmark = pytest.mark.integration


# --- archivos de prueba --------------------------------------------------------------


def make_jpeg(path: Path, *, make: str = "SAMSUNG", taken: str = "2024:06:12 15:30:12") -> Path:
    exif = Image.Exif()
    exif[0x010F] = make
    exif[0x0110] = "SM-A546E"
    exif.get_ifd(0x8769)[0x9003] = taken
    Image.new("RGB", (32, 24), "teal").save(path, exif=exif)
    return path


def make_pdf(path: Path, text: str, *, author: str = "Banco Estado") -> Path:
    """PDF mínimo escrito a mano, con una página de texto y metadatos."""
    content = f"BT /F1 12 Tf 20 100 Td ({text}) Tj ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 200] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        f"<< /Title (Cartola) /Author ({author}) /Producer (GeneradorPDF 1.0) >>".encode(),
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, obj in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + obj + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R /Info 6 0 R >>\n" % (len(objects) + 1)
    out += b"startxref\n%d\n%%%%EOF\n" % xref
    path.write_bytes(bytes(out))
    return path


def make_mp3(path: Path) -> Path:
    """Tramas MPEG vacías (válidas para mutagen) con etiquetas ID3."""
    frame = bytes([0xFF, 0xFB, 0x90, 0x64]) + bytes(413)
    path.write_bytes(frame * 20)
    tags = EasyID3()  # type: ignore[no-untyped-call]
    tags["artist"] = "Three Days Grace"
    tags["album"] = "One-X"
    tags["genre"] = "Rock"
    tags.save(path)
    return path


# --- extractores ------------------------------------------------------------------------


def test_jpeg_exif(tmp_path: Path) -> None:
    path = make_jpeg(tmp_path / "IMG_20240612_153012.jpg")
    assert mime_type(path) == "image/jpeg"
    meta = extract_metadata(path, "image/jpeg")["exif"]
    assert meta == {"marca": "SAMSUNG", "modelo": "SM-A546E", "año_captura": "2024"}


def test_pdf_metadata_and_text(tmp_path: Path) -> None:
    path = make_pdf(tmp_path / "doc.pdf", "Cartola mensual cuenta corriente cartola")
    assert mime_type(path) == "application/pdf"
    meta = extract_metadata(path, "application/pdf")["pdf"]
    assert meta["autor"] == "Banco Estado"
    assert meta["productor"] == "GeneradorPDF 1.0"
    assert meta["paginas"] == "1"
    assert "Cartola mensual" in meta["texto"]


def test_audio_tags(tmp_path: Path) -> None:
    path = make_mp3(tmp_path / "cancion.mp3")
    assert mime_type(path) == "audio/mpeg"
    meta = extract_metadata(path, "audio/mpeg")["audio"]
    assert meta == {"artista": "Three Days Grace", "album": "One-X", "genero": "Rock"}


@pytest.mark.parametrize(
    ("name", "mime", "content"),
    [
        ("rota.jpg", "image/jpeg", b"\xff\xd8\xff\xe0 truncada"),
        ("rota.pdf", "application/pdf", b"%PDF-1.4\n1 0 obj << basura"),
        ("rota.mp3", "audio/mpeg", b"ID3\x04\x00\x00\x00\x00\x00\x10basura"),
        ("vacia.pdf", "application/pdf", b""),
    ],
)
def test_corrupt_files_give_no_metadata(
    tmp_path: Path, name: str, mime: str, content: bytes
) -> None:
    path = tmp_path / name
    path.write_bytes(content)
    assert extract_metadata(path, mime) == {}
    features = extract_features(path)  # nunca lanza por contenido dañado
    assert f"ext:{name.rsplit('.', 1)[1]}" in features.tokens()


def test_files_without_metadata(tmp_path: Path) -> None:
    plain = tmp_path / "notas.txt"
    plain.write_text("hola", encoding="utf-8")
    Image.new("RGB", (8, 8)).save(tmp_path / "sin_exif.png")
    assert extract_metadata(plain, mime_type(plain)) == {}
    assert extract_metadata(tmp_path / "sin_exif.png", "image/png") == {}


def test_mime_falls_back_to_extension(tmp_path: Path) -> None:
    path = tmp_path / "datos.csv"
    path.write_bytes(b"")  # libmagic: inode/x-empty
    assert mime_type(path) == "text/csv"


def test_origin_domain_from_xattr(tmp_path: Path) -> None:
    path = tmp_path / "cartola.pdf"
    path.write_bytes(b"x")
    assert origin_domain(path) is None
    try:
        os.setxattr(path, "user.xdg.origin.url", b"https://www.bancoestado.cl/cartolas/1")
    except OSError:
        pytest.skip("el sistema de archivos no admite xattrs de usuario")
    assert origin_domain(path) == "bancoestado.cl"
    assert "origen:bancoestado.cl" in extract_features(path).tokens()


# --- características completas ----------------------------------------------------------------


def test_tokens_combine_all_signals(tmp_path: Path) -> None:
    path = make_pdf(tmp_path / "CartolaBanco_2024-05 (1).pdf", "Cartola cuenta corriente cuenta")
    tokens = extract_features(path).tokens()
    for expected in (
        "ext:pdf",
        "mime:application/pdf",
        "tipo:application",
        "tamaño:<10K",
        "pal:cartola",
        "pal:banco",
        "fecha:AAAA-MM",
        "año:2024",
        "copia",
        "pdf.autor:banco_estado",
        "texto:cuenta",
    ):
        assert expected in tokens
    assert len(tokens) == len(set(tokens))
    assert tokens.index("texto:cuenta") < tokens.index("texto:cartola")  # más frecuente primero


def test_without_metadata_does_not_open_file(tmp_path: Path) -> None:
    path = make_pdf(tmp_path / "informe.pdf", "texto")
    path.chmod(0)  # sin permiso de lectura: basta con stat
    try:
        features = extract_features(path, with_metadata=False)
    finally:
        path.chmod(0o600)
    assert features.mime == "application/pdf"  # por extensión
    assert features.metadata == {}


def test_json_round_trip(tmp_path: Path) -> None:
    path = make_jpeg(tmp_path / "foto_2024-06-12_v2 (1).jpg")
    features = extract_features(path)
    assert FileFeatures.from_json(features.to_json()) == features


# --- caché en el índice --------------------------------------------------------------------------


def _all(
    db: Database, root: Path, *, stats: FeatureStats | None = None, with_metadata: bool = True
) -> dict[str, FileFeatures]:
    found = indexed_features(db, [root], stats=stats, with_metadata=with_metadata)
    return {f.path.name: f.features for f in found}


def test_features_cached_and_invalidated(db: Database, tmp_path: Path) -> None:
    root = tmp_path / "u"
    root.mkdir()
    make_pdf(root / "a.pdf", "uno")
    b = make_pdf(root / "b.pdf", "dos")
    Scanner(db, ScanSettings(), mounts=()).scan(root)

    stats = FeatureStats()
    first = _all(db, root, stats=stats)
    assert stats.extracted == 2
    stats = FeatureStats()
    assert _all(db, root, stats=stats) == first
    assert stats.extracted == 0

    make_pdf(b, "tres", author="Otro")
    Scanner(db, ScanSettings(), mounts=()).scan(root)
    stats = FeatureStats()
    again = _all(db, root, stats=stats)
    assert stats.extracted == 1
    assert again["b.pdf"].metadata["pdf"]["autor"] == "Otro"


def test_new_features_version_recomputes(
    db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "u"
    root.mkdir()
    make_pdf(root / "a.pdf", "uno")
    Scanner(db, ScanSettings(), mounts=()).scan(root)
    _all(db, root)
    monkeypatch.setattr(store_module, "FEATURES_VERSION", 999)
    stats = FeatureStats()
    _all(db, root, stats=stats)
    assert stats.extracted == 1
    with db.session() as s:
        assert s.scalar(select(File.features_version)) == 999


def test_changed_since_scan_is_skipped(db: Database, tmp_path: Path) -> None:
    root = tmp_path / "u"
    root.mkdir()
    a = make_pdf(root / "a.pdf", "uno")
    Scanner(db, ScanSettings(), mounts=()).scan(root)
    os.utime(a, ns=(1, 1))
    stats = FeatureStats()
    assert _all(db, root, stats=stats) == {}
    assert stats.stale == 1


def test_light_extraction_is_not_cached(db: Database, tmp_path: Path) -> None:
    root = tmp_path / "u"
    root.mkdir()
    make_pdf(root / "a.pdf", "uno")
    Scanner(db, ScanSettings(), mounts=()).scan(root)
    _all(db, root, with_metadata=False)
    with db.session() as s:
        assert s.scalar(select(File.features)) is None
