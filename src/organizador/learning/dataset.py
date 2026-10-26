"""Conjunto de entrenamiento: archivos ya organizados → carpeta en la que están.

La premisa es que dónde tiene el usuario sus archivos indica dónde quiere los nuevos,
pero esa señal tiene ruido, así que se filtra:

- **proyectos de software** (repositorios, entornos virtuales, juegos con mods): su
  estructura interna no la decide el usuario, la decide la herramienta. Se excluyen
  completos;
- **carpetas vigiladas** (Descargas): es justo lo que está sin organizar;
- **profundidad limitada**: la clase es la carpeta truncada a ``max_depth`` niveles bajo
  su raíz, para no tener cientos de clases con dos archivos cada una;
- **clases pequeñas** (menos de ``min_examples``) y **copias exactas** (mismo BLAKE3).

Las correcciones del usuario (``Feedback``) se añaden con más peso.
"""

from __future__ import annotations

import logging
import random
from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import func, select

from organizador.core.config import LearningSettings
from organizador.features import FileFeatures
from organizador.features.store import FeatureStats, indexed_features
from organizador.storage import Database
from organizador.storage.index import under_roots
from organizador.storage.models import Directory, Feedback, File

log = logging.getLogger(__name__)

# Archivos que delatan un proyecto de software (su carpeta y lo que cuelga de ella).
PROJECT_FILE_MARKERS = frozenset(
    {
        "pyvenv.cfg",
        "pyproject.toml",
        "setup.py",
        "package.json",
        "Cargo.toml",
        "go.mod",
        "pom.xml",
        "build.gradle",
        "build.gradle.kts",
        "CMakeLists.txt",
        "meson.build",
        "composer.json",
        "Gemfile",
        "mix.exs",
        "manifest.json",  # extensiones y modpacks
        "instance.cfg",  # instancias de Minecraft (Prism/MultiMC)
    }
)
# Directorios ocultos que el escáner no indexa, así que se comprueban en el disco.
PROJECT_DIR_MARKERS = (".git", ".hg", ".svn")
SEED = 20261026


@dataclass(frozen=True, slots=True)
class Example:
    path: Path
    label: str
    features: FileFeatures
    weight: float = 1.0


@dataclass(slots=True)
class DatasetStats:
    files: int = 0
    in_projects: int = 0
    duplicates: int = 0
    small_classes: int = 0
    capped: int = 0
    feedback: int = 0
    project_dirs: list[str] = field(default_factory=list)


@dataclass(slots=True)
class Dataset:
    examples: list[Example]
    stats: DatasetStats

    @property
    def labels(self) -> list[str]:
        return sorted({e.label for e in self.examples})


def label_for(path: Path, roots: Sequence[Path], max_depth: int) -> str | None:
    """Carpeta destino de ``path``: su carpeta truncada a ``max_depth`` bajo la raíz."""
    for root in sorted(roots, key=lambda r: len(r.parts), reverse=True):
        if path.is_relative_to(root):
            parts = path.parent.relative_to(root).parts[: max(0, max_depth)]
            return str(root.joinpath(*parts))
    return None


def find_project_dirs(db: Database, roots: Sequence[Path]) -> list[str]:
    """Carpetas raíz de proyectos de software bajo ``roots`` (las más externas)."""
    with db.session() as session:
        dirs = list(session.scalars(select(Directory.path).where(under_roots(roots))))
        marked = set(
            session.scalars(
                select(Directory.path)
                .join(File)
                .where(under_roots(roots), File.name.in_(PROJECT_FILE_MARKERS))
            )
        )
    for d in dirs:
        if d not in marked and any((Path(d) / m).exists() for m in PROJECT_DIR_MARKERS):
            marked.add(d)
    outermost: list[str] = []
    prefixes: tuple[str, ...] = ()
    for d in sorted(marked):  # el orden alfabético deja cada padre antes que sus hijos
        if not d.startswith(prefixes):
            outermost.append(d)
            prefixes = (*prefixes, d.rstrip("/") + "/")
    return outermost


def _feedback_examples(db: Database, settings: LearningSettings) -> list[Example]:
    with db.session() as session:
        rows = session.execute(
            select(Feedback.file_path, Feedback.chosen, Feedback.features).where(
                Feedback.chosen.is_not(None), Feedback.features.is_not(None)
            )
        ).all()
    # El filtro SQL ya excluye los nulos; la comprobación es para el tipado.
    return [
        Example(Path(path), chosen, FileFeatures.from_json(raw), settings.feedback_weight)
        for path, chosen, raw in rows
        if chosen is not None and raw is not None
    ]


def build_dataset(
    db: Database,
    roots: Sequence[Path],
    settings: LearningSettings,
    *,
    exclude: Sequence[Path] = (),
    feature_stats: FeatureStats | None = None,
) -> Dataset:
    """Ejemplos de entrenamiento a partir del índice (hay que escanear ``roots`` antes)."""
    resolved = [r.expanduser().resolve() for r in roots]
    excluded = [e.expanduser().resolve() for e in exclude]
    stats = DatasetStats()
    stats.project_dirs = find_project_dirs(db, resolved)
    projects = [Path(p) for p in stats.project_dirs]

    with db.session() as session:
        hashes = dict(
            session.execute(
                select(Directory.path + "/" + File.name, File.content_hash)
                .join(Directory)
                .where(under_roots(resolved), File.content_hash.is_not(None))
            ).all()
        )

    with db.session() as session:
        stats.in_projects = sum(
            session.scalar(select(func.count(File.id)).join(Directory).where(under_roots([p]))) or 0
            for p in projects
        )

    by_label: dict[str, list[Example]] = defaultdict(list)
    seen_hashes: set[str] = set()
    skip = [*projects, *excluded]
    for item in indexed_features(db, resolved, skip_dirs=skip, stats=feature_stats):
        stats.files += 1
        path = item.path
        content = hashes.get(str(path))
        if content is not None:
            if content in seen_hashes:
                stats.duplicates += 1
                continue
            seen_hashes.add(content)
        label = label_for(path, resolved, settings.max_depth)
        if label is not None:
            by_label[label].append(Example(path, label, item.features))

    rng = random.Random(SEED)
    examples: list[Example] = []
    for label in sorted(by_label):
        group = by_label[label]
        if len(group) < settings.min_examples:
            stats.small_classes += len(group)
            continue
        if len(group) > settings.max_examples_per_class:
            stats.capped += len(group) - settings.max_examples_per_class
            group = rng.sample(group, settings.max_examples_per_class)
        examples.extend(group)

    feedback = _feedback_examples(db, settings)
    stats.feedback = len(feedback)
    examples.extend(feedback)
    counts = Counter(e.label for e in examples)
    log.info(
        "Conjunto de entrenamiento: %d ejemplos, %d clases, %s", len(examples), len(counts), stats
    )
    return Dataset(examples, stats)
