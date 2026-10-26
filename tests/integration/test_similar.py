from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from PIL import Image, ImageFilter
from sqlalchemy import select

from organizador.actions import ActionService
from organizador.actions.errors import SourceChangedError
from organizador.cli import main
from organizador.core.config import DuplicateSettings, ScanSettings
from organizador.duplicates import (
    KeepPolicy,
    SimilarGroup,
    SimilarImageFinder,
    SimilarReport,
    trash_similar,
)
from organizador.duplicates.similar import cluster, hamming, image_info
from organizador.scanner import Scanner
from organizador.storage import Database
from organizador.storage.models import IMAGE_UNREADABLE, File

pytestmark = pytest.mark.integration


def scene(seed: int, size: tuple[int, int] = (800, 600)) -> Image.Image:
    """Imagen «natural» reproducible: ruido de baja frecuencia suavizado."""
    rng = np.random.default_rng(seed)
    noise = (rng.random((12, 16, 3)) * 255).astype("uint8")
    return (
        Image.fromarray(noise)
        .resize(size, Image.Resampling.BICUBIC)
        .filter(ImageFilter.GaussianBlur(6))
    )


def _save(img: Image.Image, path: Path, **kw: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, **kw)
    return path


def _find(db: Database, root: Path, max_distance: int = 4) -> SimilarReport:
    Scanner(db, ScanSettings(), mounts=()).scan(root)
    settings = DuplicateSettings(image_max_distance=max_distance)
    return SimilarImageFinder(db, settings, KeepPolicy([], [])).find([root])


def _names(report: SimilarReport) -> list[set[str]]:
    return [{f.path.name for f in g.files} for g in report.groups]


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return tmp_path / "Imágenes"


# --- agrupación -----------------------------------------------------------------


def test_resized_and_recompressed_are_grouped(db: Database, root: Path) -> None:
    base = scene(1)
    _save(base, root / "original.png")
    _save(base.resize((400, 300)), root / "mitad.png")
    _save(base, root / "recomprimida.jpg", quality=30)
    _save(base.resize((200, 150)), root / "miniatura.webp", quality=50)
    _save(scene(2), root / "otra.png")
    _save(scene(3), root / "otra-mas.jpg")

    report = _find(db, root)

    assert _names(report) == [{"original.png", "mitad.png", "recomprimida.jpg", "miniatura.webp"}]
    group = report.groups[0]
    assert group.keep.path.name == "original.png"
    # recomprimida.jpg también es 800×600: gana el formato sin pérdida.
    assert group.keep_reason == "misma resolución y formato sin pérdida"
    assert all(group.distance_to_keep(f) <= 4 for f in group.files)


def test_different_images_not_grouped(db: Database, root: Path) -> None:
    for seed in range(12):
        _save(scene(100 + seed), root / f"{seed}.png")
    assert _find(db, root).groups == []


def test_identical_files_are_left_to_exact_search(db: Database, root: Path) -> None:
    a = _save(scene(1), root / "a.png")
    (root / "copia.png").write_bytes(a.read_bytes())
    assert _find(db, root).groups == []


def test_same_resolution_keeps_least_compressed(db: Database, root: Path) -> None:
    base = scene(4)
    _save(base, root / "alta.jpg", quality=95)
    _save(base, root / "baja.jpg", quality=40)
    (group,) = _find(db, root).groups
    assert group.keep.path.name == "alta.jpg"
    assert group.keep_reason == "misma resolución y menos compresión"


def test_exif_rotation_is_applied(db: Database, root: Path) -> None:
    upright = scene(5, (600, 800))
    _save(upright, root / "vertical.png")
    # Guardada «tumbada» con la etiqueta EXIF que indica girarla 90° al mostrarla.
    exif = Image.Exif()
    exif[0x0112] = 6
    _save(upright.transpose(Image.Transpose.ROTATE_90), root / "camara.jpg", exif=exif, quality=90)

    info = image_info(root / "camara.jpg")
    assert (info.width, info.height) == (600, 800)
    assert _names(_find(db, root)) == [{"vertical.png", "camara.jpg"}]


def test_threshold_controls_grouping(db: Database, root: Path) -> None:
    base = scene(6)
    a = _save(base, root / "a.png")
    b = _save(base.rotate(8), root / "girada.png")  # parecida, pero no idéntica
    d = hamming(image_info(a).perceptual_hash, image_info(b).perceptual_hash)
    assert 0 < d < 64

    assert len(_find(db, root, max_distance=d).groups) == 1
    assert _find(db, root, max_distance=d - 1).groups == []


def test_clustering_is_not_transitive() -> None:
    a = "0000000000000000"
    b = "000000000000000f"  # a 4 bits de a
    c = "00000000000000ff"  # a 4 de b, a 8 de a
    assert cluster([a, b, c], 4) == [[0, 1]]
    assert cluster([a, b, c], 8) == [[0, 1, 2]]
    assert cluster([a], 4) == []


# --- caché e imágenes problemáticas ------------------------------------------------


def test_unreadable_image_is_remembered(db: Database, root: Path) -> None:
    _save(scene(1), root / "buena.png")
    (root / "rota.jpg").write_bytes(b"\xff\xd8\xff no es un jpeg")

    first = _find(db, root)
    assert first.stats.unreadable == 1
    with db.session() as s:
        assert s.scalar(select(File.perceptual_hash).where(File.name == "rota.jpg")) == (
            IMAGE_UNREADABLE
        )
    second = _find(db, root)
    assert second.stats.hashed == 0 and second.stats.unreadable == 1


def test_perceptual_hash_cached_and_invalidated(db: Database, root: Path) -> None:
    base = scene(7)
    _save(base, root / "a.png")
    b = _save(base.resize((400, 300)), root / "b.png")

    assert _find(db, root).stats.hashed == 2
    assert _find(db, root).stats.hashed == 0

    _save(scene(8), b)  # ahora es otra imagen
    report = _find(db, root)
    assert report.stats.hashed == 1
    assert report.groups == []


def test_only_image_extensions_are_considered(db: Database, root: Path) -> None:
    img = _save(scene(1), root / "a.png")
    (root / "notas.txt").write_bytes(img.read_bytes())  # contenido de imagen, pero no lo es
    assert _find(db, root).stats.images == 1


# --- papelera ---------------------------------------------------------------------------


def _pair(db: Database, root: Path) -> SimilarGroup:
    base = scene(9)
    _save(base, root / "grande.png")
    _save(base.resize((400, 300)), root / "pequeña.png")
    (group,) = _find(db, root).groups
    return group


def test_unconfirmed_group_cannot_be_trashed(
    db: Database, root: Path, service: ActionService
) -> None:
    group = _pair(db, root)
    with pytest.raises(TypeError, match="confirmados"):
        trash_similar(group, service)  # type: ignore[arg-type]
    assert (root / "pequeña.png").exists()


def test_confirmed_group_trashed_and_restored(
    db: Database, root: Path, service: ActionService, xdg_data: Path
) -> None:
    group = _pair(db, root)
    small = root / "pequeña.png"
    original = small.read_bytes()

    result = trash_similar(group.confirm(), service)

    assert [r.source for r in result.trashed] == [small]
    assert not small.exists() and (root / "grande.png").exists()
    service.undo(result.trashed[0].action_id)
    assert small.read_bytes() == original


def test_trash_aborts_if_keeper_changed(db: Database, root: Path, service: ActionService) -> None:
    group = _pair(db, root)
    _save(scene(10), root / "grande.png")
    with pytest.raises(SourceChangedError):
        trash_similar(group.confirm(), service)
    assert (root / "pequeña.png").exists()


def test_user_can_keep_another_image(db: Database, root: Path, service: ActionService) -> None:
    group = _pair(db, root)
    small = next(f for f in group.files if f.path.name == "pequeña.png")
    result = trash_similar(group.with_keep(small).confirm(), service)
    assert [r.source.name for r in result.trashed] == ["grande.png"]


# --- CLI --------------------------------------------------------------------------------


@pytest.fixture
def home_images(isolated_home: Path) -> Path:
    base = scene(11)
    images = isolated_home / "Imágenes"
    _save(base, images / "foto.png")
    _save(base.resize((400, 300)), images / "foto-reducida.jpg", quality=60)
    return images


def test_cli_similar_lists_candidates(
    home_images: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["similar", str(home_images)]) == 0
    out = capsys.readouterr().out
    assert "1 grupos de imágenes similares" in out
    assert re.search(r"conservar\s+\S*foto\.png\s+\(800×600", out)


def test_cli_similar_has_no_yes_flag(home_images: Path) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["similar", str(home_images), "--trash", "--yes"])
    assert exc.value.code == 2


def test_cli_similar_trash_requires_answer(
    home_images: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    answers = iter(["n"])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))
    assert main(["similar", str(home_images), "--trash"]) == 0
    assert (home_images / "foto-reducida.jpg").exists()

    answers = iter(["s"])
    assert main(["similar", str(home_images), "--trash"]) == 0
    assert not (home_images / "foto-reducida.jpg").exists()
    assert "Abre las imágenes y compáralas" in capsys.readouterr().out


def test_highest_resolution_wins(db: Database, root: Path) -> None:
    base = scene(12)
    _save(base, root / "grande.jpg", quality=50)
    _save(base.resize((400, 300)), root / "pequeña.png")  # PNG: más bytes, menos píxeles
    (group,) = _find(db, root).groups
    assert group.keep.path.name == "grande.jpg"
    assert group.keep_reason == "tiene la mayor resolución (800×600)"


def test_png_size_is_not_a_quality_signal(db: Database, root: Path) -> None:
    # Dos capturas casi iguales en PNG: el tamaño no decide; se aplica la heurística general.
    base = scene(13)
    old = _save(base, root / "captura.png")
    busy = base.copy()
    busy.paste(scene(14, (40, 30)), (10, 10))  # un detalle más: PNG algo más grande
    _save(busy, root / "captura (1).png")
    assert (root / "captura (1).png").stat().st_size > old.stat().st_size

    (group,) = _find(db, root).groups
    assert group.keep.path == old
    assert group.keep_reason == "su nombre no indica que sea una copia"
