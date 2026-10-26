from __future__ import annotations

import threading
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from organizador.actions import ActionService
from organizador.core.config import WatchSettings
from organizador.downloads import DownloadMonitor, Outcome, SuggestionQueue
from organizador.features import FileFeatures
from organizador.learning.model import Prediction, Source, Suggestion
from organizador.storage import Database
from organizador.storage.models import ActionState, SuggestionState

pytestmark = pytest.mark.integration


def fixed(folder: Path | None, probability: float, source: Source = "modelo") -> Prediction:
    if folder is None:
        return Prediction(suggestions=(), abstained=True, source="modelo", rule=None, reasons=())
    return Prediction(
        suggestions=(Suggestion(str(folder), probability),),
        abstained=False,
        source=source,
        rule=None,
        reasons=(),
    )


def by_extension(table: dict[str, Prediction]) -> Callable[[FileFeatures], Prediction]:
    return lambda f: table.get(f.name.extension, fixed(None, 0))


@pytest.fixture
def downloads(workdir: Path) -> Path:
    return workdir / "Descargas"


def monitor_for(
    db: Database,
    downloads: Path,
    classify: Callable[[FileFeatures], Prediction],
    service: ActionService | None = None,
    **settings: object,
) -> DownloadMonitor:
    mover = None if service is None else (lambda s, d: service.move(s, d).action_id)
    return DownloadMonitor(
        [downloads],
        WatchSettings(stable_seconds=1, **settings),  # type: ignore[arg-type]
        SuggestionQueue(db),
        classify,
        mover,
    )


def cycle(monitor: DownloadMonitor) -> list[Outcome]:
    monitor.reconcile(now=0)
    return monitor.process(now=10)


def test_new_file_creates_a_suggestion(db: Database, downloads: Path, workdir: Path) -> None:
    (downloads / "cartola.pdf").write_bytes(b"%PDF")
    monitor = monitor_for(db, downloads, by_extension({"pdf": fixed(workdir / "Documentos", 0.8)}))

    [outcome] = cycle(monitor)
    assert outcome.moved_to is None
    [row] = SuggestionQueue(db).pending()
    assert row.path == str(downloads / "cartola.pdf")
    assert row.destination == str(workdir / "Documentos")
    assert row.confidence == pytest.approx(0.8)
    assert (downloads / "cartola.pdf").exists()  # sugerir nunca mueve


def test_partial_downloads_wait_until_complete(db: Database, downloads: Path) -> None:
    (downloads / "video.mkv").touch()  # marcador de Firefox
    (downloads / "video.mkv.part").write_bytes(b"medio")
    (downloads / "Unconfirmed 1.crdownload").write_bytes(b"chrome")
    monitor = monitor_for(db, downloads, by_extension({}))
    assert cycle(monitor) == []
    assert SuggestionQueue(db).pending() == []

    (downloads / "video.mkv.part").replace(downloads / "video.mkv")
    (downloads / "Unconfirmed 1.crdownload").replace(downloads / "manual.pdf")
    outcomes = cycle(monitor)
    assert sorted(Path(o.suggestion.path).name for o in outcomes) == ["manual.pdf", "video.mkv"]


def test_subfolders_are_not_watched(db: Database, downloads: Path) -> None:
    (downloads / "extraido").mkdir()
    (downloads / "extraido" / "a.txt").write_text("x")
    monitor = monitor_for(db, downloads, by_extension({}))
    monitor.notice(downloads / "extraido" / "a.txt", now=0)
    assert cycle(monitor) == []


def test_restart_does_not_duplicate_suggestions(db: Database, downloads: Path) -> None:
    (downloads / "a.txt").write_text("x")
    assert len(cycle(monitor_for(db, downloads, by_extension({})))) == 1
    # Nuevo proceso (reinicio del servicio): vuelve a ver el mismo archivo.
    assert cycle(monitor_for(db, downloads, by_extension({}))) == []
    assert len(SuggestionQueue(db).pending()) == 1


def test_ignored_suggestion_stays_ignored(db: Database, downloads: Path) -> None:
    (downloads / "a.txt").write_text("x")
    [outcome] = cycle(monitor_for(db, downloads, by_extension({})))
    SuggestionQueue(db).resolve(outcome.suggestion.id, SuggestionState.IGNORED)
    assert cycle(monitor_for(db, downloads, by_extension({}))) == []


def test_replaced_file_gets_a_new_suggestion(db: Database, downloads: Path) -> None:
    path = downloads / "a.txt"
    path.write_text("x")
    monitor = monitor_for(db, downloads, by_extension({}))
    [first] = cycle(monitor)
    path.write_text("contenido nuevo")
    [second] = cycle(monitor)
    assert second.suggestion.id != first.suggestion.id
    assert [r.id for r in SuggestionQueue(db).pending()] == [second.suggestion.id]


def test_removed_file_expires_its_suggestion(db: Database, downloads: Path) -> None:
    path = downloads / "a.txt"
    path.write_text("x")
    monitor = monitor_for(db, downloads, by_extension({}))
    cycle(monitor)
    path.rename(downloads.parent / "a.txt")
    monitor.reconcile(now=20)
    assert SuggestionQueue(db).pending() == []


def test_auto_mode_moves_only_above_threshold(
    db: Database, downloads: Path, workdir: Path, service: ActionService
) -> None:
    docs = workdir / "Documentos"
    (downloads / "seguro.pdf").write_bytes(b"1")
    (downloads / "dudoso.txt").write_bytes(b"2")
    (downloads / "por_tipo.jpg").write_bytes(b"3")
    classify = by_extension(
        {
            "pdf": fixed(docs, 0.95),
            "txt": fixed(docs, 0.6),
            # Sin probabilidad calibrada: nunca se mueve solo.
            "jpg": fixed(docs, 0.0, source="tipo"),
        }
    )
    monitor = monitor_for(db, downloads, classify, service, mode="auto", auto_threshold=0.9)
    outcomes = {Path(o.suggestion.path).name: o for o in cycle(monitor)}

    assert outcomes["seguro.pdf"].moved_to == docs
    assert (docs / "seguro.pdf").exists()
    assert outcomes["seguro.pdf"].suggestion.state == SuggestionState.AUTO
    [entry] = service.history()
    assert (
        entry.state == ActionState.DONE and entry.id == outcomes["seguro.pdf"].suggestion.action_id
    )
    pending = sorted(Path(r.path).name for r in SuggestionQueue(db).pending())
    assert pending == ["dudoso.txt", "por_tipo.jpg"]
    assert (downloads / "dudoso.txt").exists()


def test_suggest_mode_never_moves(
    db: Database, downloads: Path, workdir: Path, service: ActionService
) -> None:
    (downloads / "seguro.pdf").write_bytes(b"1")
    classify = by_extension({"pdf": fixed(workdir / "Documentos", 0.99)})
    [outcome] = cycle(monitor_for(db, downloads, classify, service, mode="suggest"))
    assert outcome.moved_to is None
    assert (downloads / "seguro.pdf").exists()


def test_watchdog_events_produce_suggestion(db: Database, downloads: Path) -> None:
    monitor = DownloadMonitor(
        [downloads],
        WatchSettings(stable_seconds=0.2, reconcile_seconds=3600),
        SuggestionQueue(db),
        by_extension({}),
    )
    seen: list[Outcome] = []
    stop = threading.Event()
    thread = threading.Thread(
        target=monitor.run, args=(stop, seen.append), kwargs={"poll_seconds": 0.05}
    )
    thread.start()
    try:
        time.sleep(0.3)  # primera reconciliación (carpeta vacía) y observer en marcha
        (downloads / "nuevo.txt.part").write_text("descargando")
        (downloads / "nuevo.txt.part").rename(downloads / "nuevo.txt")
        deadline = time.monotonic() + 5
        while not seen and time.monotonic() < deadline:
            time.sleep(0.05)
    finally:
        stop.set()
        thread.join(timeout=5)
    assert [Path(o.suggestion.path).name for o in seen] == ["nuevo.txt"]
