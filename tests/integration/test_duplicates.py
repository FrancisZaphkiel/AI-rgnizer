from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy import select

from organizador.actions import ActionService
from organizador.actions.errors import SourceChangedError
from organizador.core.config import DuplicateSettings, ScanSettings
from organizador.duplicates import DuplicateFinder, DuplicateReport, KeepPolicy, trash_redundant
from organizador.duplicates import finder as finder_module
from organizador.duplicates.birthtime import birth_time_ns
from organizador.duplicates.space import disk_usage
from organizador.scanner import Scanner
from organizador.storage import Database
from organizador.storage.models import File

pytestmark = pytest.mark.integration

PARTIAL = 4096  # ventanas pequeñas para poder probar el filtro con archivos pequeños


def _write(path: Path, content: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def _find(db: Database, root: Path, **settings: int) -> DuplicateReport:
    Scanner(db, ScanSettings(), mounts=()).scan(root)
    policy = KeepPolicy(organized_dirs=[root / "Documentos"], watched_dirs=[root / "Descargas"])
    return DuplicateFinder(db, DuplicateSettings(partial_bytes=PARTIAL, **settings), policy).find(
        [root]
    )


def _groups(report: DuplicateReport) -> list[set[str]]:
    return [{f.path.name for f in g.files} for g in report.groups]


@pytest.fixture
def root(tmp_path: Path) -> Path:
    r = tmp_path / "usuario"
    for d in ("Documentos", "Descargas", "Otros"):
        (r / d).mkdir(parents=True)
    return r


# --- detección ------------------------------------------------------------------


def test_exact_duplicates_grouped_and_keeper_proposed(db: Database, root: Path) -> None:
    data = os.urandom(20_000)
    _write(root / "Documentos/informe.pdf", data)
    _write(root / "Descargas/informe (1).pdf", data)
    _write(root / "Otros/informe copia.pdf", data)
    _write(root / "Otros/unico.pdf", os.urandom(20_000))

    report = _find(db, root)

    assert _groups(report) == [{"informe.pdf", "informe (1).pdf", "informe copia.pdf"}]
    group = report.groups[0]
    assert group.keep.path == root / "Documentos/informe.pdf"
    assert {f.path.name for f in group.redundant} == {"informe (1).pdf", "informe copia.pdf"}
    assert group.recoverable_bytes == sum(disk_usage(f.path).exclusive for f in group.redundant)
    assert group.recoverable_bytes >= 2 * 20_000


def test_same_size_different_content_not_grouped(db: Database, root: Path) -> None:
    # Mismo principio y final: el hash parcial coincide, solo BLAKE3 los distingue.
    head, tail = os.urandom(PARTIAL), os.urandom(PARTIAL)
    _write(root / "Otros/a.bin", head + b"A" * 10_000 + tail)
    _write(root / "Otros/b.bin", head + b"B" * 10_000 + tail)
    # Mismo tamaño, contenido completamente distinto (descartados por el hash parcial).
    _write(root / "Otros/c.bin", os.urandom(30_000))
    _write(root / "Otros/d.bin", os.urandom(30_000))

    report = _find(db, root)

    assert report.groups == []
    assert report.stats.partial_hashed == 4
    assert report.stats.full_hashed == 2  # solo a y b pasan el filtro


def test_small_files_skip_partial_stage(db: Database, root: Path) -> None:
    _write(root / "Otros/a.txt", b"hola")
    _write(root / "Otros/b.txt", b"hola")
    report = _find(db, root)
    assert _groups(report) == [{"a.txt", "b.txt"}]
    assert report.stats.partial_hashed == 0


def test_empty_files_ignored_by_default(db: Database, root: Path) -> None:
    for name in ("a", "b", "c"):
        _write(root / f"Otros/{name}.txt", b"")
    assert _find(db, root).groups == []


def test_min_size_filters_small_files(db: Database, root: Path) -> None:
    _write(root / "Otros/a.txt", b"x" * 10)
    _write(root / "Otros/b.txt", b"x" * 10)
    assert _find(db, root, min_size=11).groups == []


def test_hardlinks_alone_are_not_duplicates(db: Database, root: Path) -> None:
    original = _write(root / "Documentos/a.bin", os.urandom(10_000))
    os.link(original, root / "Otros/a.bin")
    assert _find(db, root).groups == []


def test_hardlink_of_keeper_is_never_proposed(db: Database, root: Path) -> None:
    data = os.urandom(10_000)
    keep = _write(root / "Documentos/a.bin", data)
    os.link(keep, root / "Otros/enlace.bin")
    _write(root / "Descargas/a.bin", data)

    (group,) = _find(db, root).groups

    assert group.keep.path == keep
    assert [f.path.name for f in group.hardlinks_of_keep] == ["enlace.bin"]
    assert [f.path for f in group.redundant] == [root / "Descargas/a.bin"]
    assert group.files[0].nlink == 2 or group.files[1].nlink == 2


def test_hardlinked_copy_outside_group_frees_nothing(
    db: Database, root: Path, tmp_path: Path
) -> None:
    data = os.urandom(10_000)
    _write(root / "Documentos/a.bin", data)
    copy = _write(root / "Descargas/a.bin", data)
    os.link(copy, tmp_path / "fuera-de-la-raiz.bin")  # el inode sobrevive al borrar la copia

    (group,) = _find(db, root).groups
    assert group.redundant[0].path == copy
    assert group.recoverable_bytes == 0


def test_hashes_are_cached_and_invalidated_on_change(db: Database, root: Path) -> None:
    data = os.urandom(20_000)
    _write(root / "Otros/a.bin", data)
    b = _write(root / "Otros/b.bin", data)

    first = _find(db, root)
    assert first.stats.full_hashed == 2
    second = _find(db, root)
    assert second.stats.full_hashed == 0 and second.stats.partial_hashed == 0
    assert _groups(second) == _groups(first)

    _write(b, os.urandom(20_000))
    third = _find(db, root)
    assert third.groups == []
    assert third.stats.full_hashed == 0  # el parcial nuevo ya los distingue
    with db.session() as s:
        stored = s.scalar(select(File.content_hash).where(File.name == "b.bin"))
    assert stored is None


def test_file_changed_after_scan_is_skipped(db: Database, root: Path) -> None:
    data = os.urandom(10_000)
    _write(root / "Otros/a.bin", data)
    b = _write(root / "Otros/b.bin", data)
    Scanner(db, ScanSettings(), mounts=()).scan(root)
    os.utime(b, ns=(1, 1))  # el índice ya no refleja el disco

    policy = KeepPolicy([], [])
    report = DuplicateFinder(db, DuplicateSettings(), policy).find([root])

    assert report.groups == []
    assert report.stats.stale == 1


def test_file_changing_while_hashed_is_discarded(
    db: Database, root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = os.urandom(10_000)
    _write(root / "Otros/a.bin", data)
    b = _write(root / "Otros/b.bin", data)
    Scanner(db, ScanSettings(), mounts=()).scan(root)

    real = finder_module.hashing.full_hash

    def hash_then_touch(path: Path) -> str:
        digest = real(path)
        if path == b:
            os.utime(b, ns=(5, 5))
        return digest

    monkeypatch.setattr(finder_module.hashing, "full_hash", hash_then_touch)
    report = DuplicateFinder(db, DuplicateSettings(), KeepPolicy([], [])).find([root])

    assert report.groups == []
    with db.session() as s:
        stored = s.scalar(select(File.content_hash).where(File.name == "b.bin"))
    assert stored is None


def test_unreadable_file_counted_as_error(db: Database, root: Path) -> None:
    if os.geteuid() == 0:
        pytest.skip("root puede leer cualquier archivo")
    data = os.urandom(10_000)
    _write(root / "Otros/a.bin", data)
    _write(root / "Otros/b.bin", data)
    locked = _write(root / "Otros/c.bin", data)
    locked.chmod(0)  # antes de escanear: chmod cambia el ctime
    Scanner(db, ScanSettings(), mounts=()).scan(root)
    try:
        report = DuplicateFinder(db, DuplicateSettings(), KeepPolicy([], [])).find([root])
    finally:
        locked.chmod(0o600)
    assert _groups(report) == [{"a.bin", "b.bin"}]
    assert report.stats.errors == 1


def test_only_files_under_requested_roots(db: Database, root: Path) -> None:
    data = os.urandom(10_000)
    _write(root / "Otros/a.bin", data)
    _write(root / "Otros2/a.bin", data)  # prefijo de texto común, carpeta distinta
    Scanner(db, ScanSettings(), mounts=()).scan(root)
    finder = DuplicateFinder(db, DuplicateSettings(), KeepPolicy([], []))
    assert finder.find([root / "Otros"]).groups == []
    assert len(finder.find([root / "Otros", root / "Otros2"]).groups) == 1


def test_cancel_returns_no_groups(db: Database, root: Path) -> None:
    import threading

    data = os.urandom(10_000)
    _write(root / "Otros/a.bin", data)
    _write(root / "Otros/b.bin", data)
    Scanner(db, ScanSettings(), mounts=()).scan(root)
    cancel = threading.Event()
    cancel.set()
    report = DuplicateFinder(db, DuplicateSettings(), KeepPolicy([], [])).find(
        [root], cancel=cancel
    )
    assert report.stats.cancelled
    assert report.groups == []


_contents = st.lists(
    st.sampled_from([b"", b"a", b"b", b"ab", b"ba", b"x" * 9000, b"x" * 8999 + b"y"]).flatmap(
        lambda base: st.sampled_from([base, base + b"!", b"!" + base])
    ),
    min_size=2,
    max_size=12,
)


@settings(
    max_examples=40, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
@given(contents=_contents)
def test_no_false_positives(
    tmp_path_factory: pytest.TempPathFactory, contents: list[bytes]
) -> None:
    base = tmp_path_factory.mktemp("prop")
    db = Database(base / "t.db")
    db.migrate()
    try:
        root = base / "r"
        paths = [_write(root / f"{i}.bin", c) for i, c in enumerate(contents)]
        report = _find(db, root)
        for group in report.groups:
            assert len({f.path.read_bytes() for f in group.files}) == 1
        # Y no falta ningún duplicado real (contenido no vacío repetido).
        expected = {c for c in contents if c and contents.count(c) > 1}
        found = {g.files[0].path.read_bytes() for g in report.groups}
        assert found == expected
        assert len(paths) == len(contents)
    finally:
        db.dispose()


@pytest.mark.slow
def test_large_sparse_files(db: Database, root: Path) -> None:
    size = 1024**3 + 7  # >1 GiB, sin ocupar disco
    for name in ("a", "b", "c"):
        with (root / f"Otros/{name}.img").open("wb") as fh:
            fh.truncate(size)
    with (root / "Otros/c.img").open("r+b") as fh:
        fh.seek(size // 2)
        fh.write(b"\x01")  # mismo principio y final que a y b

    report = _find(db, root)

    assert _groups(report) == [{"a.img", "b.img"}]
    assert report.groups[0].size == size
    assert report.groups[0].recoverable_bytes == 0  # sparse: no ocupan bloques


def test_copy_with_preserved_mtime_keeps_original(db: Database, root: Path) -> None:
    original = _write(root / "Otros/carpeta larga/Techno Syndrome.mp3", os.urandom(10_000))
    time.sleep(0.01)  # btime con resolución de nanosegundos, pero por si acaso
    copy = _write(root / "Otros/musica.mp3", original.read_bytes())
    shutil.copystat(original, copy)  # como cp -p o Dolphin: misma mtime
    if birth_time_ns(copy) is None:
        pytest.skip("el sistema de archivos no guarda la fecha de creación")

    (group,) = _find(db, root).groups

    assert group.keep.path == original
    assert group.keep_reason == "se creó antes que las demás"


def test_birth_time_of_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        birth_time_ns(tmp_path / "no-existe")


# --- reflinks (Btrfs/XFS) ----------------------------------------------------------


@pytest.fixture
def reflink_dir() -> Iterator[Path]:
    """Directorio en un sistema de archivos con reflinks (tmp suele ser tmpfs)."""
    candidates = [os.environ.get("ORGANIZADOR_REFLINK_TMP"), str(Path.home() / ".cache")]
    for base in filter(None, candidates):
        if not Path(base).is_dir():
            continue
        d = Path(tempfile.mkdtemp(prefix="organizador-test-", dir=base))
        probe = _write(d / "probe", b"x" * 8192)
        ok = subprocess.run(
            ["cp", "--reflink=always", probe, d / "probe2"], capture_output=True, check=False
        )
        if ok.returncode == 0:
            yield d
            shutil.rmtree(d)  # noqa: TID251 - limpieza de un temporal propio del test
            return
        shutil.rmtree(d)  # noqa: TID251
    pytest.skip("no hay un sistema de archivos con reflinks disponible")


def test_reflinked_copies_not_counted_as_recoverable(db: Database, reflink_dir: Path) -> None:
    root = reflink_dir / "usuario"
    data = os.urandom(1024 * 1024)
    original = _write(root / "Documentos/video.mp4", data)
    subprocess.run(
        ["cp", "--reflink=always", original, root / "Documentos/video (1).mp4"], check=True
    )
    # Copia real: shutil.copyfile usaría copy_file_range, que en Btrfs ya crea un reflink.
    _write(root / "Documentos/video copia.mp4", data)

    (group,) = _find(db, root).groups

    assert len(group.redundant) == 2
    real_copy = next(f for f in group.redundant if f.path.name == "video copia.mp4")
    assert group.recoverable_bytes == real_copy.exclusive_bytes >= 1024 * 1024


# --- papelera de extremo a extremo ---------------------------------------------------


def test_trash_redundant_and_restore(
    db: Database, root: Path, service: ActionService, xdg_data: Path
) -> None:
    data = os.urandom(10_000)
    keep = _write(root / "Documentos/a.pdf", data)
    os.link(keep, root / "Otros/enlace.pdf")
    dup1 = _write(root / "Descargas/a.pdf", data)
    dup2 = _write(root / "Descargas/a (1).pdf", data)

    (group,) = _find(db, root).groups
    result = trash_redundant(group, service)

    assert result.failed == []
    assert {r.source for r in result.trashed} == {dup1, dup2}
    assert not dup1.exists() and not dup2.exists()
    assert keep.read_bytes() == data and (root / "Otros/enlace.pdf").exists()
    for r in result.trashed:
        assert r.destination.is_relative_to(xdg_data / "Trash/files")
        assert (xdg_data / f"Trash/info/{r.destination.name}.trashinfo").exists()

    for r in result.trashed:
        service.undo(r.action_id)
    assert dup1.read_bytes() == data and dup2.read_bytes() == data
    assert list((xdg_data / "Trash/info").iterdir()) == []


def test_trash_aborts_if_keeper_changed(db: Database, root: Path, service: ActionService) -> None:
    data = os.urandom(10_000)
    keep = _write(root / "Documentos/a.pdf", data)
    dup = _write(root / "Descargas/a.pdf", data)
    (group,) = _find(db, root).groups

    keep.write_bytes(b"modificado")
    with pytest.raises(SourceChangedError):
        trash_redundant(group, service)
    assert dup.exists()


def test_trash_skips_copy_modified_after_search(
    db: Database, root: Path, service: ActionService
) -> None:
    data = os.urandom(10_000)
    _write(root / "Documentos/a.pdf", data)
    changed = _write(root / "Descargas/a.pdf", data)
    intact = _write(root / "Descargas/a (1).pdf", data)
    (group,) = _find(db, root).groups

    changed.write_bytes(os.urandom(10_000))
    result = trash_redundant(group, service)

    assert [r.source for r in result.trashed] == [intact]
    assert [f.path for f, _ in result.failed] == [changed]
    assert isinstance(result.failed[0][1], SourceChangedError)
    assert changed.exists()


def test_user_selected_keeper_is_respected(
    db: Database, root: Path, service: ActionService
) -> None:
    data = os.urandom(10_000)
    proposed = _write(root / "Documentos/a.pdf", data)
    chosen = _write(root / "Descargas/a.pdf", data)
    (group,) = _find(db, root).groups
    assert group.keep.path == proposed

    group = group.with_keep(next(f for f in group.files if f.path == chosen))
    result = trash_redundant(group, service)

    assert [r.source for r in result.trashed] == [proposed]
    assert chosen.exists()
