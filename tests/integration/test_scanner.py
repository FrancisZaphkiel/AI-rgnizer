from __future__ import annotations

import os
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import func, select

from organizador.core.config import ScanSettings
from organizador.scanner import Scanner
from organizador.scanner.mounts import mount_points
from organizador.storage import Database
from organizador.storage.models import Directory, File, ScanError

# Nota: se usa el sistema de archivos real (tmp_path) en lugar de pyfakefs porque
# SQLite accede al disco desde código C, fuera del alcance de pyfakefs.
pytestmark = pytest.mark.integration


@pytest.fixture
def db(tmp_path: Path) -> Iterator[Database]:
    database = Database(tmp_path / "index.db")
    database.migrate()
    yield database
    database.dispose()


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    root = tmp_path / "usuario"
    (root / "Documentos/Banco").mkdir(parents=True)
    (root / "Imágenes").mkdir()
    (root / ".oculto").mkdir()
    (root / "proyecto/node_modules/pkg").mkdir(parents=True)
    (root / "Documentos/Banco/2024-05_cartola.pdf").write_bytes(b"pdf")
    (root / "Documentos/notas.txt").write_text("hola", encoding="utf-8")
    (root / "Imágenes/foto.jpg").write_bytes(b"\xff\xd8")
    (root / ".oculto/secreto.txt").write_text("x", encoding="utf-8")
    (root / "proyecto/node_modules/pkg/index.js").write_text("", encoding="utf-8")
    (root / "proyecto/main.py").write_text("", encoding="utf-8")
    return root


def _indexed_names(db: Database) -> set[str]:
    with db.session() as s:
        return set(s.scalars(select(File.name)))


def test_scan_indexes_and_applies_exclusions(db: Database, tree: Path) -> None:
    stats = Scanner(db, ScanSettings(), mounts=()).scan(tree)
    assert _indexed_names(db) == {"2024-05_cartola.pdf", "notas.txt", "foto.jpg", "main.py"}
    assert stats.files_new == 4
    assert stats.errors == 0


def test_scan_does_not_modify_files(db: Database, tree: Path) -> None:
    before = {p: p.stat().st_mtime_ns for p in tree.rglob("*")}
    Scanner(db, ScanSettings(), mounts=()).scan(tree)
    assert {p: p.stat().st_mtime_ns for p in tree.rglob("*")} == before


def test_incremental_scan_only_reprocesses_changed(db: Database, tree: Path) -> None:
    scanner = Scanner(db, ScanSettings(), mounts=())
    scanner.scan(tree)
    with db.session() as s:
        row = s.scalars(select(File).where(File.name == "notas.txt")).one()
        row.content_hash = "x" * 64
        other = s.scalars(select(File).where(File.name == "main.py")).one()
        other.content_hash = "y" * 64

    (tree / "Documentos/notas.txt").write_text("cambiado", encoding="utf-8")
    stats = scanner.scan(tree)

    assert stats.files_new == 0
    assert stats.files_changed == 1
    with db.session() as s:
        hashes = dict(s.execute(select(File.name, File.content_hash)).all())
    assert hashes["notas.txt"] is None  # invalidado
    assert hashes["main.py"] == "y" * 64  # conservado


def test_removed_files_and_dirs_disappear_from_index(db: Database, tree: Path) -> None:
    scanner = Scanner(db, ScanSettings(), mounts=())
    scanner.scan(tree)
    (tree / "Imágenes/foto.jpg").rename(tree / "foto_movida.jpg")
    (tree / "Imágenes").rmdir()
    stats = scanner.scan(tree)
    assert stats.files_removed == 1
    assert "foto_movida.jpg" in _indexed_names(db)
    with db.session() as s:
        assert s.scalar(select(Directory).where(Directory.path.endswith("Imágenes"))) is None


def test_symlinks_not_followed_by_default(db: Database, tree: Path, tmp_path: Path) -> None:
    outside = tmp_path / "fuera"
    outside.mkdir()
    (outside / "externo.txt").write_text("", encoding="utf-8")
    (tree / "enlace_dir").symlink_to(outside)
    (tree / "enlace_archivo").symlink_to(tree / "proyecto/main.py")
    Scanner(db, ScanSettings(), mounts=()).scan(tree)
    names = _indexed_names(db)
    assert "externo.txt" not in names
    assert "enlace_archivo" not in names


def test_symlink_cycle_terminates(db: Database, tree: Path) -> None:
    (tree / "Documentos/bucle").symlink_to(tree)
    stats = Scanner(db, ScanSettings(follow_symlinks=True), mounts=()).scan(tree)
    assert not stats.cancelled
    assert stats.skipped >= 1


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignora los permisos")
def test_unreadable_dir_is_recorded_and_preserved(db: Database, tree: Path) -> None:
    scanner = Scanner(db, ScanSettings(), mounts=())
    scanner.scan(tree)
    locked = tree / "Documentos/Banco"
    locked.chmod(0o000)
    try:
        stats = scanner.scan(tree)
    finally:
        locked.chmod(0o755)
    assert stats.errors == 1
    # Lo que no se pudo listar no se elimina del índice.
    assert "2024-05_cartola.pdf" in _indexed_names(db)
    with db.session() as s:
        assert s.scalar(select(func.count()).select_from(ScanError)) == 1


def test_mount_points_are_not_crossed(db: Database, tree: Path) -> None:
    stats = Scanner(db, ScanSettings(), mounts=[tree / "Imágenes"]).scan(tree)
    assert "foto.jpg" not in _indexed_names(db)
    assert stats.skipped >= 1


def test_cancelled_scan_does_not_remove_entries(db: Database, tree: Path) -> None:
    scanner = Scanner(db, ScanSettings(), mounts=())
    scanner.scan(tree)
    cancel = threading.Event()
    cancel.set()
    stats = scanner.scan(tree, cancel=cancel)
    assert stats.cancelled
    assert stats.files_removed == 0
    assert len(_indexed_names(db)) == 4


def test_mountinfo_parsing(tmp_path: Path) -> None:
    info = tmp_path / "mountinfo"
    info.write_text(
        "22 1 0:21 / / rw - btrfs /dev/nvme0n1p2 rw\n"
        "40 22 0:35 / /run/media/usuario/Mi\\040Disco rw - ext4 /dev/sda1 rw\n",
        encoding="utf-8",
    )
    assert mount_points(info) == {Path("/"), Path("/run/media/usuario/Mi Disco")}
