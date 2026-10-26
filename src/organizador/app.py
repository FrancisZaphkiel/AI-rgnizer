"""Contexto de la aplicación y operaciones compartidas por la CLI y el servicio.

Nada aquí escribe en la terminal: devuelve resultados y usa ``logging``; quien llama
decide cómo mostrarlos.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import func, select

from organizador.actions import ActionService
from organizador.core.config import Settings, load_settings
from organizador.core.paths import AppPaths
from organizador.downloads import SuggestionNotFoundError, SuggestionQueue
from organizador.features import FileFeatures, extract_features
from organizador.learning.dataset import build_dataset, find_project_dirs, label_for
from organizador.learning.decide import decide
from organizador.learning.folders import FolderNames
from organizador.learning.model import DestinationModel, NotEnoughDataError, Prediction
from organizador.learning.store import IncompatibleModelError, ModelNotFoundError, ModelStore
from organizador.storage import Database
from organizador.storage.models import (
    ActionJournal,
    DownloadSuggestion,
    Feedback,
    SuggestionState,
)

log = logging.getLogger(__name__)


@dataclass(slots=True)
class AppContext:
    paths: AppPaths
    settings: Settings
    db: Database
    # Cómo avisar de las acciones interrumpidas que se recuperan (la CLI usa print).
    report: Callable[[str], None] = log.warning

    @classmethod
    def open(cls) -> AppContext:
        paths = AppPaths.from_env()
        paths.ensure()
        db = Database(paths.database_file)
        db.migrate()
        return cls(paths, load_settings(paths), db)

    def actions(self, origin: str = "cli") -> ActionService:
        service = ActionService(
            self.db, self.settings.actions, self.paths.state_dir / "actions.lock", origin=origin
        )
        for outcome in service.recover():
            self.report(f"recuperada {outcome.action_id[:8]}: {outcome.state} — {outcome.detail}")
        return service

    def models(self) -> ModelStore:
        return ModelStore(self.paths.data_dir / "models", keep=self.settings.learning.keep_models)


# --- aprendizaje ------------------------------------------------------------------------


def load_model(ctx: AppContext) -> DestinationModel | None:
    """Modelo activo; sin él solo se usan las reglas de config.toml y los destinos por tipo."""
    try:
        return ctx.models().load()
    except (ModelNotFoundError, IncompatibleModelError) as exc:
        log.warning("%s; solo se usan tus reglas y los destinos por tipo", exc)
        return None


def learning_exclude(ctx: AppContext) -> list[Path]:
    return [*ctx.settings.watched_dirs, *map(Path, ctx.settings.learning.exclude)]


def folder_names(ctx: AppContext, roots: Sequence[Path] | None = None) -> FolderNames:
    """Carpetas indexadas que pueden ser destino (sin proyectos ni carpetas excluidas)."""
    roots = (
        list(roots) if roots is not None else [r.expanduser() for r in ctx.settings.indexed_dirs]
    )
    projects = [Path(p) for p in find_project_dirs(ctx.db, roots)]
    return FolderNames.from_index(ctx.db, roots, exclude=[*learning_exclude(ctx), *projects])


def retrain_from_index(ctx: AppContext) -> int | None:
    """Reentrena con lo ya indexado (sin escanear); devuelve la versión o ``None``."""
    roots = [r.expanduser() for r in ctx.settings.indexed_dirs if r.expanduser().is_dir()]
    if not roots:
        return None
    exclude = learning_exclude(ctx)
    dataset = build_dataset(ctx.db, roots, ctx.settings.learning, exclude=exclude)
    try:
        model = DestinationModel.train(dataset, ctx.settings.learning)
    except NotEnoughDataError:
        return None
    return ctx.models().save(model)


def record_choice(
    ctx: AppContext,
    path: Path,
    folder: Path,
    features: FileFeatures,
    load: Callable[[AppContext], DestinationModel | None] = load_model,
) -> str:
    """Registra que ``path`` va en ``folder``, hace que el modelo lo aprenda y lo describe."""
    learning = ctx.settings.learning
    # El modelo aprende carpetas a la profundidad de max_depth bajo las raíces indexadas.
    roots = [r.expanduser().resolve() for r in ctx.settings.indexed_dirs]
    label = label_for(folder / path.name, roots, learning.max_depth) or str(folder)
    model = load(ctx) if ctx.models().current_version() else None
    prediction = decide(features, model, learning, folder_names(ctx))
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
        return f"aprendido: {path.name} → {label} (modelo {ctx.models().save(model)})"
    if (pending or 0) >= learning.retrain_after and (version := retrain_from_index(ctx)):
        return f"{label}: {pending} archivos elegidos, reentrenado (modelo {version})"
    return (
        f"anotado: {path.name} → {label}; "
        f"lo aprenderé tras {learning.retrain_after} archivos o con «organizador train»"
    )


def classifier(
    ctx: AppContext, load: Callable[[AppContext], DestinationModel | None] = load_model
) -> Callable[[FileFeatures], Prediction]:
    """Decide destinos recargando el modelo cuando ``train`` o ``mv`` guardan otra versión."""
    store = ctx.models()
    loaded: dict[str, Any] = {"version": None, "model": None, "folders": folder_names(ctx)}

    def classify(features: FileFeatures) -> Prediction:
        version = store.current_version()
        if version != loaded["version"]:
            loaded["version"] = version
            loaded["model"] = load(ctx) if version else None
            loaded["folders"] = folder_names(ctx)
        return decide(features, loaded["model"], ctx.settings.learning, loaded["folders"])

    return classify


# --- sugerencias de las carpetas vigiladas ------------------------------------------------


class SuggestionError(ValueError):
    """La sugerencia ya está resuelta o no se puede aplicar."""


class SuggestionMissingError(SuggestionError):
    """No hay ninguna sugerencia con ese id."""


@dataclass(frozen=True, slots=True)
class Accepted:
    suggestion_id: int
    action_id: str
    source: Path
    destination: Path
    learned: str


def pending_suggestion(ctx: AppContext, suggestion_id: int) -> DownloadSuggestion:
    try:
        row = SuggestionQueue(ctx.db).get(suggestion_id)
    except SuggestionNotFoundError as exc:
        raise SuggestionMissingError(str(exc)) from exc
    if row.state != SuggestionState.PENDING:
        raise SuggestionError(f"la sugerencia {row.id} ya está resuelta ({row.state})")
    return row


def accept_suggestion(
    ctx: AppContext,
    suggestion_id: int,
    folder: Path | None = None,
    *,
    actions: ActionService | None = None,
) -> Accepted:
    """Mueve el archivo a ``folder`` (o al destino sugerido) y aprende la elección.

    Raises:
        SuggestionError: sin destino, resuelta o el archivo ya no existe.
        ActionError: el movimiento falló (el archivo sigue donde estaba).
    """
    row = pending_suggestion(ctx, suggestion_id)
    target = folder.expanduser() if folder else (Path(row.destination) if row.destination else None)
    if target is None:
        raise SuggestionError("sin destino sugerido; indica la carpeta")
    source = Path(row.path)
    if not source.is_file():
        SuggestionQueue(ctx.db).resolve(row.id, SuggestionState.GONE)
        raise SuggestionError(f"{source} ya no existe")
    target.mkdir(parents=True, exist_ok=True)
    features = extract_features(source)
    result = (actions or ctx.actions()).move(source, target)
    SuggestionQueue(ctx.db).resolve(
        row.id, SuggestionState.ACCEPTED, chosen=str(target), action_id=result.action_id
    )
    learned = record_choice(ctx, result.destination, result.destination.parent.resolve(), features)
    return Accepted(row.id, result.action_id, result.source, result.destination, learned)


def ignore_suggestion(ctx: AppContext, suggestion_id: int) -> None:
    pending_suggestion(ctx, suggestion_id)
    SuggestionQueue(ctx.db).resolve(suggestion_id, SuggestionState.IGNORED)


def undo_action(ctx: AppContext, action_id: str, *, actions: ActionService | None = None) -> str:
    """Deshace una acción; si movió una descarga, su sugerencia vuelve a estar pendiente.

    Devuelve el id de la acción de deshacer. Lanza ``ActionError`` si no se puede.
    """
    service = actions or ctx.actions()
    result = service.undo(action_id)
    with ctx.db.session() as s:
        original = s.scalar(
            select(ActionJournal.reverts_id).where(ActionJournal.id == result.action_id)
        )
        for row in s.scalars(
            select(DownloadSuggestion).where(DownloadSuggestion.action_id == original)
        ):
            row.state = SuggestionState.PENDING
            row.chosen = row.action_id = None
            st = result.destination.stat()
            row.mtime_ns = st.st_mtime_ns  # la restauración puede redondear el mtime
    return result.action_id
