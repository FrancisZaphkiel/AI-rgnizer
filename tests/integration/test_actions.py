from __future__ import annotations

import os
import sys
from collections.abc import Callable
from pathlib import Path
from types import FrameType
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from organizador.actions import ActionService, fsops
from organizador.actions.errors import (
    ConcurrentOperationError,
    DestinationOccupiedError,
    InvalidTargetError,
    NotUndoableError,
    SourceChangedError,
)
from organizador.actions.journal import Journal
from organizador.core.config import ActionSettings
from organizador.storage import Database
from organizador.storage.models import ActionKind, ActionState

pytestmark = pytest.mark.integration


class Crash(BaseException):
    """No hereda de Exception: ningún ``except OSError`` del código la intercepta."""


def _write(path: Path, content: bytes = b"contenido") -> Path:
    path.write_bytes(content)
    return path


def _new_service(db: Database, tmp_path: Path) -> ActionService:
    return ActionService(db, ActionSettings(), tmp_path / "state" / "actions.lock", origin="test")


def _state(db: Database, action_id: str) -> ActionState:
    entry = Journal(db).get(action_id)
    assert entry is not None
    return entry.state


def _simulate_cross_device(monkeypatch: pytest.MonkeyPatch, original: Path) -> None:
    """Hace que renombrar ``original`` falle con EXDEV, como entre subvolúmenes Btrfs."""
    real = fsops.move_no_replace

    def fake(src: Path, dst: Path) -> None:
        if src == original:
            raise fsops.CrossDeviceError(18, "Invalid cross-device link", str(src))
        real(src, dst)

    monkeypatch.setattr(fsops, "move_no_replace", fake)


# --- mover / renombrar ------------------------------------------------------------


def test_move_and_undo_restore_exact_path(service: ActionService, workdir: Path) -> None:
    src = _write(workdir / "Descargas/informe.pdf")
    result = service.move(src, workdir / "Documentos")
    assert not src.exists()
    assert result.destination == workdir / "Documentos/informe.pdf"
    service.undo(result.action_id)
    assert src.read_bytes() == b"contenido"
    assert not result.destination.exists()


_names = st.text(
    alphabet=st.characters(codec="utf-8", exclude_characters="/\0"),
    min_size=1,
    max_size=40,
).filter(lambda n: n not in {".", ".."} and len(os.fsencode(n)) <= 200)


@settings(
    max_examples=40, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
@given(name=_names, new_name=_names)
def test_rename_undo_roundtrip_property(
    service: ActionService, workdir: Path, name: str, new_name: str
) -> None:
    folder = workdir / "prop"
    folder.mkdir(exist_ok=True)
    for leftover in folder.iterdir():
        leftover.rename(workdir / f"descartado-{os.urandom(4).hex()}")
    src = _write(folder / name)
    if new_name == name:
        with pytest.raises(InvalidTargetError):
            service.rename(src, new_name)
        return
    result = service.rename(src, new_name)
    assert (folder / new_name).exists() and not src.exists()
    service.undo(result.action_id)
    assert sorted(p.name for p in folder.iterdir()) == [name]


def test_move_collision_never_overwrites(service: ActionService, workdir: Path) -> None:
    existing = _write(workdir / "Documentos/foto.jpg", b"original")
    src = _write(workdir / "Descargas/foto.jpg", b"nueva")
    result = service.move(src, workdir / "Documentos")
    assert existing.read_bytes() == b"original"
    assert result.destination.name == "foto (2).jpg"
    assert result.destination.read_bytes() == b"nueva"


def test_rename_to_existing_name_is_rejected(service: ActionService, workdir: Path) -> None:
    _write(workdir / "Documentos/b.txt", b"b")
    src = _write(workdir / "Documentos/a.txt", b"a")
    with pytest.raises(DestinationOccupiedError) as info:
        service.rename(src, "b.txt")
    assert info.value.suggestion == workdir / "Documentos/b (2).txt"
    assert src.read_bytes() == b"a"


def test_move_no_replace_is_atomic_noreplace(tmp_path: Path) -> None:
    a, b = _write(tmp_path / "a", b"a"), _write(tmp_path / "b", b"b")
    with pytest.raises(FileExistsError):
        fsops.move_no_replace(a, b)
    assert a.read_bytes() == b"a" and b.read_bytes() == b"b"


def test_symlinks_are_rejected(service: ActionService, workdir: Path) -> None:
    target = _write(workdir / "Documentos/real.txt")
    link = workdir / "Descargas/enlace.txt"
    link.symlink_to(target)
    with pytest.raises(InvalidTargetError):
        service.move(link, workdir / "Documentos")


# --- entre sistemas de archivos ------------------------------------------------


def test_cross_device_move_copies_verifies_and_removes_source(
    service: ActionService, workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = _write(workdir / "Descargas/video.mkv", os.urandom(300_000))
    payload = src.read_bytes()
    mtime = src.stat().st_mtime_ns
    _simulate_cross_device(monkeypatch, src)
    result = service.move(src, workdir / "Documentos")
    assert not src.exists()
    assert result.destination.read_bytes() == payload
    assert result.destination.stat().st_mtime_ns == mtime
    assert not any(fsops.is_own_temp(p) for p in (workdir / "Documentos").iterdir())


def test_cross_device_crash_after_copy_keeps_source_and_recovers(
    db: Database, tmp_path: Path, workdir: Path, xdg_data: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _new_service(db, tmp_path)
    src = _write(workdir / "Descargas/datos.bin", os.urandom(50_000))
    payload = src.read_bytes()
    _simulate_cross_device(monkeypatch, src)

    real_fsync = fsops.fsync_dir

    def crash_after_publish(directory: Path) -> None:
        real_fsync(directory)
        if directory == workdir / "Documentos":
            raise Crash

    monkeypatch.setattr(fsops, "fsync_dir", crash_after_publish)
    with pytest.raises(Crash):
        service.move(src, workdir / "Documentos")
    monkeypatch.setattr(fsops, "fsync_dir", real_fsync)

    # Estado tras el "corte": origen intacto y copia publicada.
    assert src.read_bytes() == payload
    outcomes = _new_service(db, tmp_path).recover()
    assert [o.state for o in outcomes] == [ActionState.DONE]
    assert not src.exists()
    assert (workdir / "Documentos/datos.bin").read_bytes() == payload


# --- recuperación tras fallos ----------------------------------------------------


def test_crash_before_move_leaves_file_in_place(
    db: Database, tmp_path: Path, workdir: Path, xdg_data: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _new_service(db, tmp_path)
    src = _write(workdir / "Descargas/a.txt")

    real = fsops.move_no_replace

    def crash(*_args: Any) -> None:
        raise Crash

    monkeypatch.setattr(fsops, "move_no_replace", crash)
    with pytest.raises(Crash):
        service.move(src, workdir / "Documentos")
    monkeypatch.setattr(fsops, "move_no_replace", real)

    outcomes = _new_service(db, tmp_path).recover()
    assert [o.state for o in outcomes] == [ActionState.FAILED]
    assert src.exists()
    assert not (workdir / "Documentos/a.txt").exists()


def _crash_on_transition_to(
    monkeypatch: pytest.MonkeyPatch, target_state: ActionState
) -> Callable[[], None]:
    real = Journal.transition

    def wrapper(self: Journal, entry_id: str, state: ActionState, **kw: Any) -> Any:
        if state is target_state:
            raise Crash
        return real(self, entry_id, state, **kw)

    monkeypatch.setattr(Journal, "transition", wrapper)
    return lambda: monkeypatch.setattr(Journal, "transition", real)


def test_crash_after_move_before_done_is_completed(
    db: Database, tmp_path: Path, workdir: Path, xdg_data: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = _write(workdir / "Descargas/b.txt")
    restore = _crash_on_transition_to(monkeypatch, ActionState.DONE)
    with pytest.raises(Crash):
        _new_service(db, tmp_path).move(src, workdir / "Documentos")
    restore()

    service = _new_service(db, tmp_path)
    outcomes = service.recover()
    assert [o.state for o in outcomes] == [ActionState.DONE]
    # La acción recuperada también se puede deshacer.
    service.undo(outcomes[0].action_id)
    assert src.exists()


def test_crash_after_trash_before_done_is_located(
    db: Database, tmp_path: Path, workdir: Path, xdg_data: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = _write(workdir / "Descargas/c.txt")
    restore = _crash_on_transition_to(monkeypatch, ActionState.DONE)
    with pytest.raises(Crash):
        _new_service(db, tmp_path).trash(src)
    restore()

    service = _new_service(db, tmp_path)
    outcomes = service.recover()
    assert [o.state for o in outcomes] == [ActionState.DONE]
    service.undo(outcomes[0].action_id)
    assert src.read_bytes() == b"contenido"


def test_recover_with_nothing_pending_is_noop(service: ActionService) -> None:
    assert service.recover() == []


# --- papelera --------------------------------------------------------------------


def test_trash_uses_freedesktop_layout_and_undo_restores(
    service: ActionService, workdir: Path, xdg_data: Path
) -> None:
    src = _write(workdir / "Descargas/duplicado (1).pdf")
    result = service.trash(src)
    files = xdg_data / "Trash/files/duplicado (1).pdf"
    info = xdg_data / "Trash/info/duplicado (1).pdf.trashinfo"
    assert result.destination == files
    assert files.exists() and not src.exists()
    content = info.read_text(encoding="utf-8")
    assert content.startswith("[Trash Info]\n")
    assert "Path=" + str(src).replace(" ", "%20").replace("(", "%28").replace(")", "%29") in content
    assert "DeletionDate=" in content

    service.undo(result.action_id)
    assert src.exists()
    assert not files.exists() and not info.exists()


def test_trash_name_collision_in_trash(
    service: ActionService, workdir: Path, xdg_data: Path
) -> None:
    first = service.trash(_write(workdir / "Descargas/x.txt", b"1"))
    second = service.trash(_write(workdir / "Descargas/x.txt", b"2"))
    assert first.destination.name == "x.txt"
    assert second.destination.name == "x 2.txt"
    assert first.destination.read_bytes() == b"1"


def test_trash_cross_device_uses_topdir_trash(
    service: ActionService, workdir: Path, xdg_data: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Simula un subvolumen Btrfs: el rename a la papelera del home da EXDEV."""
    from organizador.actions import trash as trash_mod

    src = _write(workdir / "Descargas/sub.txt")
    real = fsops.move_no_replace

    def fake(a: Path, b: Path) -> None:
        if xdg_data in b.parents:
            raise fsops.CrossDeviceError(18, "Invalid cross-device link", str(a))
        real(a, b)

    monkeypatch.setattr(fsops, "move_no_replace", fake)
    monkeypatch.setattr(trash_mod, "_device_root", lambda _p: workdir)
    result = service.trash(src)
    assert result.destination == workdir / f".Trash-{os.getuid()}/files/sub.txt"
    info = workdir / f".Trash-{os.getuid()}/info/sub.txt.trashinfo"
    assert "Path=Descargas/sub.txt" in info.read_text(encoding="utf-8")
    # El .trashinfo reservado en la papelera del home se limpió al fallar.
    assert not list((xdg_data / "Trash/info").iterdir())


# --- deshacer: rechazos seguros --------------------------------------------------------


def test_undo_refuses_if_file_modified(service: ActionService, workdir: Path) -> None:
    result = service.move(_write(workdir / "Descargas/m.txt"), workdir / "Documentos")
    result.destination.write_bytes(b"editado despues")
    with pytest.raises(SourceChangedError):
        service.undo(result.action_id)
    assert result.destination.read_bytes() == b"editado despues"


def test_undo_refuses_if_origin_occupied(service: ActionService, workdir: Path) -> None:
    src = _write(workdir / "Descargas/o.txt", b"movido")
    result = service.move(src, workdir / "Documentos")
    _write(src, b"nuevo con el mismo nombre")
    with pytest.raises(DestinationOccupiedError):
        service.undo(result.action_id)
    assert src.read_bytes() == b"nuevo con el mismo nombre"
    assert result.destination.read_bytes() == b"movido"


def test_undo_twice_is_rejected(db: Database, service: ActionService, workdir: Path) -> None:
    result = service.move(_write(workdir / "Descargas/t.txt"), workdir / "Documentos")
    undo = service.undo(result.action_id)
    assert _state(db, result.action_id) is ActionState.REVERTED
    with pytest.raises(NotUndoableError):
        service.undo(result.action_id)
    entry = Journal(db).get(undo.action_id)
    assert entry is not None and entry.kind is ActionKind.UNDO
    with pytest.raises(NotUndoableError):
        service.undo(undo.action_id)


# --- concurrencia ------------------------------------------------------------------


def test_concurrent_operation_on_same_file_is_rejected(
    service: ActionService, workdir: Path
) -> None:
    src = _write(workdir / "Descargas/k.txt")
    with service._locks.hold(src), pytest.raises(ConcurrentOperationError):
        service.move(src, workdir / "Documentos")
    assert src.exists()


def test_active_journal_entry_blocks_new_operation(
    db: Database, service: ActionService, workdir: Path
) -> None:
    from organizador.actions.fingerprint import Fingerprint

    src = _write(workdir / "Descargas/j.txt")
    Journal(db).begin(
        ActionKind.MOVE,
        src,
        workdir / "Documentos/j.txt",
        Fingerprint.of(src, hash_max_bytes=None),
        origin="otro-proceso",
    )
    with pytest.raises(ConcurrentOperationError):
        service.trash(src)


# --- auditoría de borrados ------------------------------------------------------------


def test_unlink_only_called_from_fsops(
    service: ActionService, workdir: Path, xdg_data: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ningún borrado fuera de ``organizador.actions.fsops`` (incluye ``Path.unlink``)."""
    offenders: list[str] = []
    real_unlink, real_remove = os.unlink, os.remove  # noqa: TID251

    def audit(real: Callable[..., None]) -> Callable[..., None]:
        def wrapper(path: Any, *args: Any, **kwargs: Any) -> None:
            frame: FrameType | None = sys._getframe(1)
            while frame is not None and frame.f_globals.get("__name__", "").startswith("pathlib"):
                frame = frame.f_back  # Path.unlink delega en os.unlink
            module = frame.f_globals.get("__name__", "") if frame else ""
            if module.startswith("organizador") and module != "organizador.actions.fsops":
                offenders.append(f"{module}: {path}")
            real(path, *args, **kwargs)

        return wrapper

    monkeypatch.setattr(os, "unlink", audit(real_unlink))
    monkeypatch.setattr(os, "remove", audit(real_remove))

    a = service.move(_write(workdir / "Descargas/1.txt"), workdir / "Documentos")
    service.undo(a.action_id)
    b = service.rename(workdir / "Descargas/1.txt", "2.txt")
    service.undo(b.action_id)
    c = service.trash(workdir / "Descargas/1.txt")
    service.undo(c.action_id)
    src = _write(workdir / "Descargas/x.bin", os.urandom(10_000))
    _simulate_cross_device(monkeypatch, src)
    service.move(src, workdir / "Documentos")

    assert offenders == []


def test_discard_temp_refuses_foreign_files(tmp_path: Path) -> None:
    victim = _write(tmp_path / "importante.txt")
    with pytest.raises(ValueError, match="temporal"):
        fsops.discard_temp(victim)
    with pytest.raises(ValueError, match="trashinfo"):
        fsops.remove_trashinfo(victim)
    assert victim.exists()


def test_move_on_filesystem_without_permissions_or_hardlinks(
    service: ActionService, workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """exFAT/FAT o FUSE: sin RENAME_NOREPLACE, sin link() y chmod rechazado."""
    src = _write(workdir / "Descargas/video.mkv", os.urandom(50_000))
    payload, mtime = src.read_bytes(), src.stat().st_mtime_ns
    _simulate_cross_device(monkeypatch, src)
    monkeypatch.setattr(fsops, "_renameat2", None)

    def no_link(*_a: Any, **_k: Any) -> None:
        raise PermissionError(1, "Operation not permitted")

    def no_copystat(*_a: Any, **_k: Any) -> None:
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(fsops.os, "link", no_link)
    monkeypatch.setattr(fsops.shutil, "copystat", no_copystat)
    (workdir / "Documentos/video.mkv").write_bytes(b"ocupado")

    result = service.move(src, workdir / "Documentos")
    assert result.destination.name == "video (2).mkv"
    assert result.destination.read_bytes() == payload
    assert result.destination.stat().st_mtime_ns == mtime
    assert (workdir / "Documentos/video.mkv").read_bytes() == b"ocupado"
    assert not src.exists()


def test_check_then_rename_never_overwrites(tmp_path: Path) -> None:
    src, dst = tmp_path / "a", tmp_path / "b"
    src.write_text("a")
    dst.write_text("b")
    with pytest.raises(FileExistsError):
        fsops._check_then_rename(src, dst)
    assert dst.read_text() == "b"


def test_undo_after_move_to_low_resolution_filesystem(
    service: ActionService, workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """exFAT redondea el mtime a 10 ms y FAT a 2 s: deshacer debe seguir funcionando."""
    src = _write(workdir / "Descargas/notas.txt", b"hola")
    os.utime(src, ns=(src.stat().st_atime_ns, 1_700_000_000_123_456_789))
    result = service.move(src, workdir / "Documentos")
    rounded = 1_700_000_000_000_000_000 + 2_000_000_000  # FAT redondea a 2 s hacia arriba
    os.utime(result.destination, ns=(rounded, rounded))
    service.undo(result.action_id)
    assert src.read_bytes() == b"hola"


def test_undo_refuses_changed_content_despite_tolerance(
    service: ActionService, workdir: Path
) -> None:
    src = _write(workdir / "Descargas/notas.txt", b"hola")
    result = service.move(src, workdir / "Documentos")
    st = result.destination.stat()
    result.destination.write_bytes(b"HOLA")  # mismo tamaño, otro contenido
    os.utime(result.destination, ns=(st.st_atime_ns, st.st_mtime_ns))
    with pytest.raises(SourceChangedError):
        service.undo(result.action_id)
