"""Interfaz de línea de comandos provisional."""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol, Self

from sqlalchemy import func, select

from organizador import __version__
from organizador.actions import ActionError, ActionService
from organizador.core.config import Settings, load_settings
from organizador.core.paths import AppPaths
from organizador.downloads import (
    DownloadMonitor,
    Outcome,
    SuggestionNotFoundError,
    SuggestionQueue,
)
from organizador.downloads.queue import alternatives_of
from organizador.duplicates import (
    DuplicateFinder,
    DuplicateGroup,
    KeepPolicy,
    SimilarGroup,
    SimilarImageFinder,
    trash_redundant,
    trash_similar,
)
from organizador.duplicates.names import NameGroup, SimilarNameFinder
from organizador.features import FileFeatures, extract_features
from organizador.learning.dataset import Dataset, build_dataset, find_project_dirs, label_for
from organizador.learning.decide import decide, decide_many
from organizador.learning.evaluate import evaluate
from organizador.learning.folders import FolderNames
from organizador.learning.model import DestinationModel, NotEnoughDataError, Prediction
from organizador.learning.patterns import FolderPattern, dominant_pattern, render
from organizador.learning.review import Misplaced, find_misplaced, is_in
from organizador.learning.store import IncompatibleModelError, ModelNotFoundError, ModelStore
from organizador.scanner import Scanner, ScanStats
from organizador.storage import Database
from organizador.storage.index import under_roots
from organizador.storage.models import (
    Directory,
    DownloadSuggestion,
    Feedback,
    File,
    SuggestionState,
)

log = logging.getLogger("organizador")


@dataclass(slots=True)
class AppContext:
    paths: AppPaths
    settings: Settings
    db: Database

    @classmethod
    def open(cls) -> AppContext:
        paths = AppPaths.from_env()
        paths.ensure()
        db = Database(paths.database_file)
        db.migrate()
        return cls(paths, load_settings(paths), db)

    def actions(self) -> ActionService:
        service = ActionService(
            self.db, self.settings.actions, self.paths.state_dir / "actions.lock", origin="cli"
        )
        for outcome in service.recover():
            print(f"recuperada {outcome.action_id[:8]}: {outcome.state} — {outcome.detail}")
        return service

    def models(self) -> ModelStore:
        return ModelStore(self.paths.data_dir / "models", keep=self.settings.learning.keep_models)


def _cmd_paths(_ctx: AppContext, _args: argparse.Namespace) -> int:
    paths = AppPaths.from_env()
    for name in ("config_dir", "data_dir", "state_dir", "cache_dir"):
        print(f"{name:<11} {getattr(paths, name)}")
    return 0


def _resolve_roots(ctx: AppContext, args: argparse.Namespace) -> list[Path] | None:
    """Carpetas indicadas (o ``indexed_dirs``); ``None`` si falta alguna, tras avisar de todas."""
    roots: list[Path] = [r.expanduser() for r in args.roots] or ctx.settings.indexed_dirs
    if not roots:
        print("Indica una carpeta o configura indexed_dirs en config.toml", file=sys.stderr)
        return None
    missing = [r for r in roots if not r.is_dir()]
    for root in missing:
        print(f"error: no es una carpeta: {root}", file=sys.stderr)
    if missing:
        print("Si la ruta tiene espacios, ponla entre comillas.", file=sys.stderr)
        return None
    return roots


def _cmd_scan(ctx: AppContext, args: argparse.Namespace) -> int:
    def progress(stats: ScanStats) -> None:
        print(f"\r{stats.files_seen} archivos…", end="", file=sys.stderr, flush=True)

    roots = _resolve_roots(ctx, args)
    if roots is None:
        return 2
    scanner = Scanner(ctx.db, ctx.settings.scan)
    for root in roots:
        try:
            stats = scanner.scan(root, progress=progress)
        except KeyboardInterrupt:
            print("\nescaneo cancelado", file=sys.stderr)
            return 130
        print(
            f"\r{stats.root}: {stats.files_seen} archivos "
            f"({stats.files_new} nuevos, {stats.files_changed} modificados, "
            f"{stats.files_removed} eliminados del índice), {stats.errors} errores"
        )
    return 0


def human_size(n: int) -> str:
    size = float(n)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if size < 1024 or unit == "GiB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    raise AssertionError("inalcanzable")  # pragma: no cover


def _print_group(index: int, group: DuplicateGroup) -> None:
    print(
        f"\nGrupo {index} · {len(group.files)} archivos de {human_size(group.size)} · "
        f"recuperable {human_size(group.recoverable_bytes)}"
    )
    hardlinks = set(group.hardlinks_of_keep)
    for n, f in enumerate(group.files, start=1):
        if f is group.keep:
            label, note = "conservar", f"  ({group.keep_reason})"
        elif f in hardlinks:
            label, note = "enlace", "  (hardlink de la copia conservada: no ocupa espacio)"
        else:
            label, note = "sobra", ""
        print(f"  [{n}] {label:<9} {f.path}{note}")


class _Group(Protocol):
    """Lo común a ``DuplicateGroup`` y ``SimilarGroup`` (miembros con atributo ``path``)."""

    @property
    def files(self) -> tuple[Any, ...]: ...

    @property
    def keep(self) -> Any: ...

    def with_keep(self, keep: Any) -> Self: ...


def _ask_group[G: _Group](group: G, question: str) -> G | None:
    """Confirmación por grupo: s = aceptar, n = saltar, número = conservar otro archivo."""
    while True:
        try:
            answer = input(f"  {question} [s/N/nº a conservar] ")
        except EOFError:
            return None
        answer = answer.strip().lower()
        if answer in {"s", "si", "sí", "y"}:
            return group
        if answer in {"", "n", "no"}:
            return None
        if answer.isdigit() and 1 <= int(answer) <= len(group.files):
            group = group.with_keep(group.files[int(answer) - 1])
            print(f"  conservar: {group.keep.path}")
            continue
        print("  Respuesta no válida.")


def _trash_groups(
    service: ActionService, groups: Sequence[DuplicateGroup], *, assume_yes: bool
) -> int:
    failures = trashed = 0
    for index, proposed in enumerate(groups, start=1):
        group: DuplicateGroup | None = proposed
        if not assume_yes:
            _print_group(index, proposed)
            group = _ask_group(proposed, "¿Enviar las copias sobrantes a la papelera?")
        if group is None:
            continue
        try:
            result = trash_redundant(group, service)
        except ActionError as exc:
            print(f"  grupo {index} omitido: {exc}", file=sys.stderr)
            failures += 1
            continue
        for action in result.trashed:
            print(f"  {action.action_id[:8]}  {action.source} → papelera")
        for dup, error in result.failed:
            print(f"  error con {dup.path}: {error}", file=sys.stderr)
        failures += len(result.failed)
        trashed += len(result.trashed)
    if trashed:
        print(f"{trashed} archivos enviados a la papelera. Para restaurar uno: organizador undo ID")
    return 1 if failures else 0


def _cmd_dupes(ctx: AppContext, args: argparse.Namespace) -> int:
    roots = _resolve_roots(ctx, args)
    if roots is None:
        return 2
    if not args.no_scan and _cmd_scan(ctx, argparse.Namespace(roots=roots)) != 0:
        return 1

    settings = ctx.settings
    duplicates = settings.duplicates
    if args.min_size is not None:
        duplicates = replace(duplicates, min_size=args.min_size)
    policy = KeepPolicy(settings.indexed_dirs, settings.watched_dirs)
    try:
        report = DuplicateFinder(ctx.db, duplicates, policy).find(roots)
    except KeyboardInterrupt:
        print("\nbúsqueda cancelada", file=sys.stderr)
        return 130

    interactive = args.trash and not args.yes
    if not interactive:  # en modo interactivo cada grupo se muestra al preguntar
        for index, group in enumerate(report.groups, start=1):
            _print_group(index, group)
    stats = report.stats
    print(
        f"\n{len(report.groups)} grupos de duplicados exactos; "
        f"recuperable {human_size(report.recoverable_bytes)}. "
        f"Leídos {human_size(stats.bytes_hashed)} ({stats.full_hashed} hashes completos)."
    )
    if stats.stale:
        print(f"{stats.stale} archivos cambiaron desde el escaneo y se omitieron.")
    if not args.trash:
        return 0

    return _trash_groups(ctx.actions(), report.groups, assume_yes=args.yes)


def _print_similar(index: int, group: SimilarGroup) -> None:
    print(
        f"\nCandidato {index} · {len(group.files)} imágenes parecidas · "
        f"ocupan {human_size(group.redundant_bytes)} además de la conservada"
    )
    for n, f in enumerate(group.files, start=1):
        label = "conservar" if f is group.keep else "parecida"
        detail = f"{f.width}×{f.height}, {human_size(f.size)}"
        if f is group.keep:
            detail += f"; {group.keep_reason}"
        else:
            detail += f"; distancia {group.distance_to_keep(f)}"
        print(f"  [{n}] {label:<9} {f.path}  ({detail})")


def _trash_similar_groups(service: ActionService, groups: Sequence[SimilarGroup]) -> int:
    # Sin --yes a propósito: cada grupo de similares exige una confirmación explícita.
    failures = trashed = 0
    for index, proposed in enumerate(groups, start=1):
        _print_similar(index, proposed)
        print("  Abre las imágenes y compáralas antes de confirmar: no son idénticas.")
        group = _ask_group(proposed, "¿Enviar a la papelera todas menos la conservada?")
        if group is None:
            continue
        try:
            result = trash_similar(group.confirm(), service)
        except ActionError as exc:
            print(f"  grupo {index} omitido: {exc}", file=sys.stderr)
            failures += 1
            continue
        for action in result.trashed:
            print(f"  {action.action_id[:8]}  {action.source} → papelera")
        for item, error in result.failed:
            print(f"  error con {item.path}: {error}", file=sys.stderr)
        failures += len(result.failed)
        trashed += len(result.trashed)
    if trashed:
        print(f"{trashed} archivos enviados a la papelera. Para restaurar uno: organizador undo ID")
    return 1 if failures else 0


def _cmd_similar(ctx: AppContext, args: argparse.Namespace) -> int:
    roots = _resolve_roots(ctx, args)
    if roots is None:
        return 2
    if not args.no_scan and _cmd_scan(ctx, argparse.Namespace(roots=roots)) != 0:
        return 1

    settings = ctx.settings
    duplicates = settings.duplicates
    if args.max_distance is not None:
        duplicates = replace(duplicates, image_max_distance=args.max_distance)
    policy = KeepPolicy(settings.indexed_dirs, settings.watched_dirs)
    try:
        report = SimilarImageFinder(ctx.db, duplicates, policy).find(roots)
    except KeyboardInterrupt:
        print("\nbúsqueda cancelada", file=sys.stderr)
        return 130

    if not args.trash:
        for index, group in enumerate(report.groups, start=1):
            _print_similar(index, group)
    stats = report.stats
    print(
        f"\n{len(report.groups)} grupos de imágenes similares (candidatos: revísalos antes de "
        f"borrar) entre {stats.images} imágenes; {stats.hashed} analizadas ahora, "
        f"{stats.unreadable} no legibles."
    )
    if not args.trash:
        return 0

    return _trash_similar_groups(ctx.actions(), report.groups)


def _print_names(index: int, group: NameGroup) -> None:
    kind = "mismo nombre" if group.identical_names else "nombres parecidos"
    print(f"\nIndicio {index} · {len(group.files)} archivos con {kind} (contenido distinto)")
    for n, f in enumerate(group.files, start=1):
        when = datetime.fromtimestamp(f.mtime_ns / 1e9).strftime("%Y-%m-%d %H:%M")
        score = "referencia" if n == 1 else f"parecido {f.score}%"
        print(f"  [{n}] {f.path}  ({human_size(f.size)}, {when}, {score})")


def _cmd_names(ctx: AppContext, args: argparse.Namespace) -> int:
    roots = _resolve_roots(ctx, args)
    if roots is None:
        return 2
    if not args.no_scan and _cmd_scan(ctx, argparse.Namespace(roots=roots)) != 0:
        return 1
    finder = SimilarNameFinder(
        ctx.db, min_score=args.min_score, min_size=ctx.settings.duplicates.min_size
    )
    report = finder.find(roots)
    for index, group in enumerate(report.groups, start=1):
        _print_names(index, group)
    print(
        f"\n{len(report.groups)} grupos de nombres parecidos entre {report.files} archivos.\n"
        "Un nombre parecido no significa que sobre un archivo (pueden ser versiones distintas):"
        "\nrevísalos y, si decides eliminar alguno, usa: organizador trash ARCHIVO"
    )
    return 0


def _cmd_mv(ctx: AppContext, args: argparse.Namespace) -> int:
    dest_dir: Path = args.dest_dir.expanduser()
    if args.parents:
        dest_dir.mkdir(parents=True, exist_ok=True)
    # Características antes de mover: describen el archivo tal como llegó.
    features = extract_features(args.source) if args.learn and args.source.is_file() else None
    result = ctx.actions().move(args.source, dest_dir)
    print(f"{result.action_id[:8]}  {result.source} → {result.destination}")
    if features is not None:
        _record_choice(ctx, result.destination, result.destination.parent.resolve(), features)
    return 0


def _cmd_rename(ctx: AppContext, args: argparse.Namespace) -> int:
    result = ctx.actions().rename(args.source, args.new_name)
    print(f"{result.action_id[:8]}  {result.source} → {result.destination}")
    return 0


def _cmd_trash(ctx: AppContext, args: argparse.Namespace) -> int:
    service = ctx.actions()
    for source in args.sources:
        result = service.trash(source)
        print(f"{result.action_id[:8]}  {result.source} → papelera")
    return 0


def _cmd_undo(ctx: AppContext, args: argparse.Namespace) -> int:
    result = ctx.actions().undo(args.action_id)
    print(f"{result.action_id[:8]}  restaurado {result.destination}")
    return 0


def _cmd_history(ctx: AppContext, args: argparse.Namespace) -> int:
    for entry in ctx.actions().history(args.limit):
        when = entry.created_at.astimezone().strftime("%Y-%m-%d %H:%M")
        target = entry.destination or "-"
        print(
            f"{entry.id[:8]}  {when}  {entry.kind:<6} {entry.state:<11} {entry.source} → {target}"
        )
    return 0


def _cmd_recover(ctx: AppContext, _args: argparse.Namespace) -> int:
    ctx.actions()  # la recuperación se ejecuta al crear el servicio
    return 0


# --- aprendizaje (Fase 3) ---------------------------------------------------------------


def _learning_dataset(ctx: AppContext, args: argparse.Namespace) -> Dataset | None:
    """Escanea (salvo ``--no-scan``) y construye el conjunto de datos; ``None`` si falla."""
    roots = _resolve_roots(ctx, args)
    if roots is None:
        return None
    if not args.no_scan and _cmd_scan(ctx, argparse.Namespace(roots=roots)) != 0:
        return None
    # Las carpetas vigiladas (Descargas) son lo que hay que organizar, no un ejemplo.
    exclude = _learning_exclude(ctx)
    dataset = build_dataset(ctx.db, roots, ctx.settings.learning, exclude=exclude)
    stats = dataset.stats
    print(
        f"{len(dataset.examples)} ejemplos en {len(dataset.labels)} carpetas "
        f"({stats.feedback} correcciones); ignorados: {stats.in_projects} en "
        f"{len(stats.project_dirs)} proyectos de software, {stats.duplicates} duplicados, "
        f"{stats.small_classes} carpetas con menos de {ctx.settings.learning.min_examples} archivos"
    )
    return dataset


def _cmd_train(ctx: AppContext, args: argparse.Namespace) -> int:
    dataset = _learning_dataset(ctx, args)
    if dataset is None:
        return 2
    try:
        model = DestinationModel.train(dataset, ctx.settings.learning)
    except NotEnoughDataError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    version = ctx.models().save(model)
    print(f"Modelo {version} guardado: {len(model.classes)} carpetas, {len(model.rules)} reglas.")
    for rule in sorted(model.rules.values(), key=lambda r: -r.support)[:10]:
        print(f"  regla {rule.token} → {rule.label}  ({rule.hits}/{rule.support})")
    return 0


def _cmd_evaluate(ctx: AppContext, args: argparse.Namespace) -> int:
    dataset = _learning_dataset(ctx, args)
    if dataset is None:
        return 2
    try:
        result = evaluate(dataset, ctx.settings.learning)
    except NotEnoughDataError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(
        f"Entrenado con {result.n_train}, evaluado con {result.n_test} archivos no vistos.\n"
        f"  cobertura {result.coverage:.1%} (se abstiene en {result.abstention:.1%})\n"
        f"  top-1 {result.top1_accuracy:.1%} · top-3 {result.top3_accuracy:.1%}"
        f" (sobre las sugerencias no abstenidas)\n"
        f"  {result.by_rule} decididas por regla, {result.by_rule_correct} correctas\n"
        f"  error de calibración (ECE) {result.ece:.3f}"
    )
    print("  confianza    archivos  acierto")
    for b in result.bins:
        print(
            f"  {b.low:.0%}–{b.high:.0%}  {b.count:>9}  {b.accuracy:>6.0%}"
            f"  (dice {b.confidence:.0%})"
        )
    return 0


_SOURCE_TEXT = {
    "configuración": "por tu regla en config.toml",
    "regla": "aprendido",
    "modelo": "sugerencia del modelo",
    "carpeta": "hay una carpeta con ese nombre",
    "tipo": "destino por defecto para este tipo de archivo",
}


def _print_prediction(path: Path, prediction: Prediction) -> None:
    print(f"\n{path}")
    if prediction.abstained:
        print("  sin clasificar: se queda donde está (muévelo con «organizador mv» y aprenderé)")
    for n, s in enumerate(prediction.suggestions, start=1):
        first = n == 1 and not prediction.abstained
        marker = "→" if first else " "
        shown = "  —" if first and prediction.source in {"configuración", "carpeta", "tipo"} else ""
        print(f"  {marker} {shown or f'{s.probability:>4.0%}'}  {s.label}")
    if prediction.abstained:
        return
    detail = _SOURCE_TEXT[prediction.source]
    if prediction.rule is not None:
        r = prediction.rule
        detail += f": {r.hits} de {r.support} archivos con {r.token} están en esa carpeta"
    elif prediction.reasons:
        detail += " (" + ", ".join(token for token, _ in prediction.reasons) + ")"
    print(f"  {detail}")
    best = prediction.best
    if best is not None and is_in(path.expanduser().resolve(), best.label):
        print("  ✓ ya está en su carpeta")


def _load_model(ctx: AppContext) -> DestinationModel | None:
    """Modelo activo; sin él solo se usan las reglas de config.toml y los destinos por tipo."""
    try:
        return ctx.models().load()
    except (ModelNotFoundError, IncompatibleModelError) as exc:
        print(f"aviso: {exc}; solo se usan tus reglas y los destinos por tipo", file=sys.stderr)
        return None


def _learning_exclude(ctx: AppContext) -> list[Path]:
    return [*ctx.settings.watched_dirs, *map(Path, ctx.settings.learning.exclude)]


def _folder_names(ctx: AppContext, roots: Sequence[Path] | None = None) -> FolderNames:
    """Carpetas indexadas que pueden ser destino (sin proyectos ni carpetas excluidas)."""
    roots = (
        list(roots) if roots is not None else [r.expanduser() for r in ctx.settings.indexed_dirs]
    )
    projects = [Path(p) for p in find_project_dirs(ctx.db, roots)]
    return FolderNames.from_index(ctx.db, roots, exclude=[*_learning_exclude(ctx), *projects])


def _cmd_suggest(ctx: AppContext, args: argparse.Namespace) -> int:
    model = _load_model(ctx)
    files = [f for f in args.files if f.is_file()]
    for missing in sorted(set(args.files) - set(files)):
        print(f"error: no es un archivo: {missing}", file=sys.stderr)
    features = [extract_features(f) for f in files]
    predictions = decide_many(features, model, ctx.settings.learning, _folder_names(ctx))
    for path, prediction in zip(files, predictions, strict=True):
        _print_prediction(path, prediction)
        _print_name_proposal(ctx, path, prediction)
    return 0 if len(files) == len(args.files) else 2


def _retrain_from_index(ctx: AppContext) -> int | None:
    """Reentrena con lo ya indexado (sin escanear); devuelve la versión o ``None``."""
    roots = [r.expanduser() for r in ctx.settings.indexed_dirs if r.expanduser().is_dir()]
    if not roots:
        return None
    exclude = _learning_exclude(ctx)
    dataset = build_dataset(ctx.db, roots, ctx.settings.learning, exclude=exclude)
    try:
        model = DestinationModel.train(dataset, ctx.settings.learning)
    except NotEnoughDataError:
        return None
    return ctx.models().save(model)


def _record_choice(ctx: AppContext, path: Path, folder: Path, features: FileFeatures) -> None:
    """Registra que ``path`` va en ``folder`` y hace que el modelo lo aprenda."""
    learning = ctx.settings.learning
    # El modelo aprende carpetas a la profundidad de max_depth bajo las raíces indexadas.
    roots = [r.expanduser().resolve() for r in ctx.settings.indexed_dirs]
    label = label_for(folder / path.name, roots, learning.max_depth) or str(folder)
    model = _load_model(ctx) if ctx.models().current_version() else None
    prediction = decide(features, model, learning, _folder_names(ctx))
    suggested = prediction.best.label if prediction.best else None
    with ctx.db.session() as s:
        s.add(
            Feedback(
                file_path=str(path),
                suggested=suggested,
                chosen=label,
                accepted=suggested == label,
                confidence=prediction.suggestions[0].probability
                if prediction.suggestions
                else None,
                features=features.to_json(),
            )
        )
        s.flush()
        pending = s.scalar(
            select(func.count()).select_from(Feedback).where(Feedback.chosen == label)
        )
    if model is not None and model.learn(features, label):
        print(f"  aprendido: {path.name} → {label} (modelo {ctx.models().save(model)})")
        return
    if (pending or 0) >= learning.retrain_after and (version := _retrain_from_index(ctx)):
        print(f"  {label}: {pending} archivos elegidos, reentrenado (modelo {version})")
        return
    print(
        f"  anotado: {path.name} → {label}; "
        f"lo aprenderé tras {learning.retrain_after} archivos o con «organizador train»"
    )


def _cmd_teach(ctx: AppContext, args: argparse.Namespace) -> int:
    path: Path = args.file.expanduser().resolve()
    folder: Path = args.folder.expanduser().resolve()
    if not path.is_file() or not folder.is_dir():
        print("error: indica un archivo existente y una carpeta existente", file=sys.stderr)
        return 2
    _record_choice(ctx, path, folder, extract_features(path))
    return 0


def _ask_move(item: Misplaced) -> str | None:
    """Destino elegido para un archivo mal ubicado, o ``None`` para dejarlo."""
    options = [s.label for s in item.prediction.suggestions]
    while True:
        try:
            answer = input(f"  ¿Mover a {item.target}? [s/N/nº de alternativa] ").strip().lower()
        except EOFError:
            return None
        if answer in {"s", "si", "sí", "y"}:
            return item.target
        if answer in {"", "n", "no"}:
            return None
        if answer.isdigit() and 1 <= int(answer) <= len(options):
            return options[int(answer) - 1]
        print("  Respuesta no válida.")


def _cmd_review(ctx: AppContext, args: argparse.Namespace) -> int:
    dataset = _learning_dataset(ctx, args)
    if dataset is None:
        return 2
    roots = [r.expanduser() for r in args.roots] or [
        r.expanduser() for r in ctx.settings.indexed_dirs
    ]
    folders = _folder_names(ctx, roots)

    def progress(done: int, total: int) -> None:
        print(f"\rrevisando {done}/{total}…", end="", file=sys.stderr, flush=True)

    report = find_misplaced(dataset, ctx.settings.learning, folders, progress=progress)
    found = report.misplaced
    print("", file=sys.stderr)
    service = ctx.actions() if args.move else None
    for index, item in enumerate(found, start=1):
        print(f"\n[{index}] posible mala ubicación (ahora en {item.current})")
        _print_prediction(item.path, item.prediction)
        if service is None:
            continue
        target = _ask_move(item)
        if target is None:
            continue
        Path(target).mkdir(parents=True, exist_ok=True)
        features = extract_features(item.path)
        result = service.move(item.path, Path(target))
        print(f"  {result.action_id[:8]}  {result.source} → {result.destination}")
        _record_choice(ctx, result.destination, result.destination.parent.resolve(), features)
    print(f"\n{len(found)} archivos posiblemente mal ubicados de {report.reviewed} revisados.")
    if found and not args.move:
        print("Para moverlos uno a uno, con confirmación: organizador review --move")
    return 0


def _names_by_folder(ctx: AppContext, roots: Sequence[Path]) -> dict[str, list[str]]:
    """Nombres de archivo de cada carpeta indexada bajo ``roots`` (sin subcarpetas)."""
    with ctx.db.session() as session:
        rows = session.execute(
            select(Directory.path, File.name).join(File).where(under_roots(roots))
        ).all()
    out: dict[str, list[str]] = {}
    for folder, name in rows:
        out.setdefault(folder, []).append(name)
    return out


def _print_name_proposal(ctx: AppContext, path: Path, prediction: Prediction) -> None:
    best = prediction.best
    if best is None or is_in(path.expanduser().resolve(), best.label):
        return
    names = _names_by_folder(ctx, [Path(best.label)]).get(best.label, [])
    found = dominant_pattern(names)
    if found is None:
        return
    when = datetime.fromtimestamp(path.stat().st_mtime)
    proposed = render(found.pattern, path.name, when)
    if proposed and proposed != path.name:
        print(
            f"  nombre según la carpeta ({found.support} de {found.total} siguen "
            f"{found.pattern}): {proposed}"
        )


def _print_pattern(folder: str, found: FolderPattern) -> None:
    print(f"\n{folder}")
    print(f"  {found.pattern}  ·  {found.support} de {found.total} archivos ({found.fraction:.0%})")
    for example in found.examples:
        print(f"    {example}")


def _cmd_patterns(ctx: AppContext, args: argparse.Namespace) -> int:
    roots = _resolve_roots(ctx, args)
    if roots is None:
        return 2
    if not args.no_scan and _cmd_scan(ctx, argparse.Namespace(roots=roots)) != 0:
        return 1
    skipped = [*_learning_exclude(ctx), *map(Path, find_project_dirs(ctx.db, roots))]
    skipped = [p.expanduser().resolve() for p in skipped]
    shown = 0
    for folder, names in sorted(_names_by_folder(ctx, roots).items()):
        if any(Path(folder).is_relative_to(p) for p in skipped):
            continue
        if (found := dominant_pattern(names)) is not None:
            _print_pattern(folder, found)
            shown += 1
    print(f"\n{shown} carpetas con un patrón de nombres claro.")
    return 0


def _cmd_model(ctx: AppContext, args: argparse.Namespace) -> int:
    store = ctx.models()
    if args.action == "revert":
        try:
            store.revert(args.version)
        except (ModelNotFoundError, IncompatibleModelError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        print(f"Modelo activo: {args.version}")
        return 0
    infos = store.list()
    if not infos:
        print("No hay modelos entrenados: ejecuta «organizador train».")
    for info in infos:
        when = info.trained_at.astimezone().strftime("%Y-%m-%d %H:%M")
        mark = "*" if info.current else " "
        print(
            f"{mark} {info.version:>4}  {when}  {info.n_examples} ejemplos, "
            f"{info.n_classes} carpetas, {info.n_rules} reglas"
        )
    return 0


Command = Callable[[AppContext, argparse.Namespace], int]


def _add_learning_commands(add: Callable[[str, Command, str], argparse.ArgumentParser]) -> None:
    for name, command, help_text in (
        ("train", _cmd_train, "aprende dónde guardas cada tipo de archivo"),
        ("evaluate", _cmd_evaluate, "mide la precisión del modelo con archivos que no vio"),
    ):
        p = add(name, command, help_text)
        p.add_argument("roots", nargs="*", type=Path)
        p.add_argument("--no-scan", action="store_true", help="usa el índice sin reescanear")

    p = add("review", _cmd_review, "busca archivos posiblemente mal ubicados")
    p.add_argument("roots", nargs="*", type=Path)
    p.add_argument("--no-scan", action="store_true", help="usa el índice sin reescanear")
    p.add_argument("--move", action="store_true", help="pregunta archivo por archivo si moverlo")

    p = add("patterns", _cmd_patterns, "muestra cómo nombras los archivos de cada carpeta")
    p.add_argument("roots", nargs="*", type=Path)
    p.add_argument("--no-scan", action="store_true", help="usa el índice sin reescanear")

    p = add("suggest", _cmd_suggest, "sugiere carpetas para archivos (no los mueve)")
    p.add_argument("files", nargs="+", type=Path)

    p = add("teach", _cmd_teach, "corrige al modelo: ARCHIVO va en CARPETA (no lo mueve)")
    p.add_argument("file", type=Path)
    p.add_argument("folder", type=Path)

    p = add("model", _cmd_model, "versiones del modelo")
    model_sub = p.add_subparsers(dest="action", required=True)
    model_sub.add_parser("list", help="lista las versiones guardadas")
    revert = model_sub.add_parser("revert", help="vuelve a una versión anterior")
    revert.add_argument("version", type=int)


def _classifier(ctx: AppContext) -> Callable[[FileFeatures], Prediction]:
    """Decide destinos recargando el modelo cuando ``train`` o ``mv`` guardan otra versión."""
    store = ctx.models()
    loaded: dict[str, Any] = {"version": None, "model": None, "folders": _folder_names(ctx)}

    def classify(features: FileFeatures) -> Prediction:
        version = store.current_version()
        if version != loaded["version"]:
            loaded["version"] = version
            loaded["model"] = _load_model(ctx) if version else None
            loaded["folders"] = _folder_names(ctx)
        return decide(features, loaded["model"], ctx.settings.learning, loaded["folders"])

    return classify


def _print_suggestion(row: DownloadSuggestion) -> None:
    if row.state == SuggestionState.AUTO:
        print(f"[{row.id}] {row.path}\n      movido a {row.chosen} ({row.confidence:.0%})")
        return
    print(f"[{row.id}] {row.path}")
    if row.destination is None:
        print("      sin clasificar: elige carpeta con «organizador accept ID --to CARPETA»")
    else:
        uncalibrated = row.source in {"configuración", "carpeta", "tipo"}
        shown = "" if uncalibrated else f"{row.confidence:.0%}, "
        print(f"      → {row.destination} ({shown}{_SOURCE_TEXT.get(row.source or '', '')})")
    others = [label for label, _ in alternatives_of(row) if label != row.destination]
    if others:
        print("        otras: " + ", ".join(others[:2]))


def _cmd_watch(ctx: AppContext, args: argparse.Namespace) -> int:
    settings = ctx.settings.watch
    if args.auto:
        settings = replace(settings, mode="auto")
    dirs = [d.expanduser() for d in (args.dirs or ctx.settings.watched_dirs)]
    service = ctx.actions()

    def mover(source: Path, folder: Path) -> str:
        return service.move(source, folder).action_id

    monitor = DownloadMonitor(dirs, settings, SuggestionQueue(ctx.db), _classifier(ctx), mover)

    def show(outcome: Outcome) -> None:
        _print_suggestion(outcome.suggestion)
        sys.stdout.flush()

    if args.once:
        monitor.reconcile()
        if monitor.waiting:
            time.sleep(settings.stable_seconds)
        for outcome in monitor.process(time.monotonic() + settings.stable_seconds):
            show(outcome)
        return 0
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    mode = "automático" if settings.mode == "auto" else "solo sugerencias"
    print(f"Vigilando {', '.join(map(str, monitor.dirs))} ({mode}). Ctrl+C para salir.")
    monitor.run(stop, show)
    return 0


def _cmd_pending(ctx: AppContext, _args: argparse.Namespace) -> int:
    queue = SuggestionQueue(ctx.db)
    queue.expire_missing()
    rows = queue.pending()
    for row in rows:
        _print_suggestion(row)
    if not rows:
        print("No hay sugerencias pendientes.")
    return 0


def _pending_row(ctx: AppContext, suggestion_id: int) -> DownloadSuggestion | None:
    try:
        row = SuggestionQueue(ctx.db).get(suggestion_id)
    except SuggestionNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return None
    if row.state != SuggestionState.PENDING:
        print(f"error: la sugerencia {row.id} ya está resuelta ({row.state})", file=sys.stderr)
        return None
    return row


def _cmd_accept(ctx: AppContext, args: argparse.Namespace) -> int:
    row = _pending_row(ctx, args.id)
    if row is None:
        return 2
    target = args.to.expanduser() if args.to else row.destination and Path(row.destination)
    if not target:
        print("error: sin destino sugerido; indica la carpeta con --to", file=sys.stderr)
        return 2
    source = Path(row.path)
    if not source.is_file():
        SuggestionQueue(ctx.db).resolve(row.id, SuggestionState.GONE)
        print(f"error: {source} ya no existe", file=sys.stderr)
        return 1
    target.mkdir(parents=True, exist_ok=True)
    features = extract_features(source)
    result = ctx.actions().move(source, target)
    SuggestionQueue(ctx.db).resolve(
        row.id, SuggestionState.ACCEPTED, chosen=str(target), action_id=result.action_id
    )
    print(f"{result.action_id[:8]}  {result.source} → {result.destination}")
    _record_choice(ctx, result.destination, result.destination.parent.resolve(), features)
    return 0


def _cmd_ignore(ctx: AppContext, args: argparse.Namespace) -> int:
    code = 0
    for suggestion_id in args.ids:
        if _pending_row(ctx, suggestion_id) is None:
            code = 2
            continue
        SuggestionQueue(ctx.db).resolve(suggestion_id, SuggestionState.IGNORED)
        print(f"[{suggestion_id}] ignorada: el archivo se queda donde está")
    return code


def _add_watch_commands(add: Callable[[str, Command, str], argparse.ArgumentParser]) -> None:
    p = add("watch", _cmd_watch, "vigila Descargas y sugiere destino a lo que llega")
    p.add_argument("dirs", nargs="*", type=Path, help="por defecto, watched_dirs de config.toml")
    p.add_argument("--once", action="store_true", help="revisa una vez y termina")
    p.add_argument(
        "--auto", action="store_true", help="mueve si la confianza llega a watch.auto_threshold"
    )

    add("pending", _cmd_pending, "sugerencias pendientes de las carpetas vigiladas")

    p = add("accept", _cmd_accept, "mueve el archivo de una sugerencia y lo aprende")
    p.add_argument("id", type=int)
    p.add_argument("--to", type=Path, metavar="CARPETA", help="otra carpeta en vez de la sugerida")

    p = add("ignore", _cmd_ignore, "descarta sugerencias (el archivo no se mueve)")
    p.add_argument("ids", nargs="+", type=int)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="organizador", description=__doc__)
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("-v", "--verbose", action="store_true", help="registro detallado")
    sub = parser.add_subparsers(dest="command", required=True)

    def add(name: str, func: Command, help_text: str) -> argparse.ArgumentParser:
        p = sub.add_parser(name, help=help_text)
        p.set_defaults(func=func)
        return p

    add("paths", _cmd_paths, "muestra las rutas XDG de la aplicación")

    p = add("scan", _cmd_scan, "indexa carpetas (solo lectura)")
    p.add_argument("roots", nargs="*", type=Path)

    p = add(
        "dupes", _cmd_dupes, "busca duplicados exactos y, opcionalmente, los envía a la papelera"
    )
    p.add_argument("roots", nargs="*", type=Path)
    p.add_argument("--no-scan", action="store_true", help="usa el índice sin reescanear")
    p.add_argument("--min-size", type=int, metavar="BYTES", help="ignora archivos más pequeños")
    p.add_argument(
        "--trash", action="store_true", help="ofrece enviar las copias sobrantes a la papelera"
    )
    p.add_argument("--yes", action="store_true", help="con --trash, no pide confirmación")

    p = add("similar", _cmd_similar, "busca imágenes casi idénticas (candidatos a revisar)")
    p.add_argument("roots", nargs="*", type=Path)
    p.add_argument("--no-scan", action="store_true", help="usa el índice sin reescanear")
    p.add_argument(
        "--max-distance",
        type=int,
        metavar="BITS",
        help="distancia de Hamming máxima entre pHash (0-64; por defecto, la de config.toml)",
    )
    p.add_argument(
        "--trash",
        action="store_true",
        help="pregunta grupo por grupo si enviar las parecidas a la papelera",
    )

    p = add("names", _cmd_names, "muestra archivos con nombres parecidos (solo indicios)")
    p.add_argument("roots", nargs="*", type=Path)
    p.add_argument("--no-scan", action="store_true", help="usa el índice sin reescanear")
    p.add_argument(
        "--min-score",
        type=int,
        default=90,
        metavar="0-100",
        help="parecido mínimo entre nombres (por defecto 90)",
    )

    _add_learning_commands(add)
    _add_watch_commands(add)

    p = add("mv", _cmd_mv, "mueve un archivo a una carpeta (sin sobrescribir) y lo aprende")
    p.add_argument("source", type=Path)
    p.add_argument("dest_dir", type=Path)
    p.add_argument("-p", "--parents", action="store_true", help="crea la carpeta si no existe")
    p.add_argument(
        "--no-learn", dest="learn", action="store_false", help="no aprender de este movimiento"
    )

    p = add("rename", _cmd_rename, "renombra un archivo")
    p.add_argument("source", type=Path)
    p.add_argument("new_name")

    p = add("trash", _cmd_trash, "envía archivos a la papelera del sistema")
    p.add_argument("sources", nargs="+", type=Path)

    p = add("undo", _cmd_undo, "deshace una acción (id o prefijo)")
    p.add_argument("action_id")

    p = add("history", _cmd_history, "historial de acciones")
    p.add_argument("-n", "--limit", type=int, default=30)

    add("recover", _cmd_recover, "completa o revierte acciones interrumpidas")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    ctx = AppContext.open()
    try:
        result: int = args.func(ctx, args)
    except ActionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        ctx.db.dispose()
    return result


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
