from __future__ import annotations

import socket
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from organizador.core.config import LearningSettings, ScanSettings
from organizador.features import extract_features
from organizador.learning import store as store_module
from organizador.learning.dataset import Dataset, build_dataset, label_for
from organizador.learning.evaluate import evaluate
from organizador.learning.model import DestinationModel, NotEnoughDataError
from organizador.learning.rules import apply_rules
from organizador.learning.store import IncompatibleModelError, ModelNotFoundError, ModelStore
from organizador.scanner import Scanner
from organizador.storage import Database
from organizador.storage.models import Feedback

pytestmark = pytest.mark.integration

SETTINGS = LearningSettings(min_examples=3, rule_min_support=5)
BANK_WORDS = ["cartola", "estado de cuenta", "cartola mensual", "movimientos cuenta"]
COURSE_WORDS = ["calidad software", "pruebas unitarias", "proceso calidad", "tdd calidad"]


def _write(path: Path, content: bytes = b"x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


@pytest.fixture
def home(tmp_path: Path) -> Path:
    """Usuario sintético con costumbres claras."""
    h = tmp_path / "home"
    for i in range(12):
        words = BANK_WORDS[i % len(BANK_WORDS)].replace(" ", "_")
        bank = h / f"Documentos/Finanzas/Banco/{words}_2024-{i % 12 + 1:02d}.pdf"
        _write(bank, b"%PDF" + bytes([i]))
        topic = COURSE_WORDS[i % len(COURSE_WORDS)]
        _write(h / f"Documentos/Universidad/Calidad/{topic} clase {i}.pdf", b"%PDF-c" + bytes([i]))
        _write(h / f"Música/Artista {i}/canción {i}.mp3", b"ID3" + bytes([i]))
        _write(h / f"Imágenes/Viajes/IMG_2024061{i % 10}_1530{i:02d}.jpg", b"\xff\xd8" + bytes([i]))
    # Proyecto de software: su estructura no debe aprenderse.
    for i in range(30):
        _write(h / f"Documentos/proyecto/src/modulo_{i}.py")
    _write(h / "Documentos/proyecto/pyproject.toml")
    (h / "Documentos/otro_repo/.git").mkdir(parents=True)
    for i in range(10):
        _write(h / f"Documentos/otro_repo/datos_{i}.csv")
    # Carpeta con muy pocos archivos y una copia exacta.
    _write(h / "Documentos/Varios/suelto.txt")
    _write(h / "Documentos/Finanzas/Banco/copia exacta.pdf", b"%PDF" + bytes([0]))
    # Descargas: sin organizar, nunca es una etiqueta.
    for i in range(5):
        _write(h / f"Descargas/cartola_2025-0{i + 1}.pdf", b"%PDF-d" + bytes([i]))
    return h


def _roots(home: Path) -> list[Path]:
    return [home / d for d in ("Documentos", "Música", "Imágenes", "Descargas")]


@pytest.fixture
def dataset(db: Database, home: Path) -> Dataset:
    for root in _roots(home):
        Scanner(db, ScanSettings(), mounts=()).scan(root)
    return build_dataset(db, _roots(home), SETTINGS, exclude=[home / "Descargas"])


@pytest.fixture
def model(dataset: Dataset) -> DestinationModel:
    return DestinationModel.train(dataset, SETTINGS)


def _predict(model: DestinationModel, path: Path) -> str | None:
    best = model.predict(extract_features(path)).best
    return best.label if best else None


# --- conjunto de datos ----------------------------------------------------------------


def test_label_is_truncated_folder(tmp_path: Path) -> None:
    root = tmp_path / "Documentos"
    assert label_for(root / "a/b/c/d.pdf", [root], 2) == str(root / "a/b")
    assert label_for(root / "a/d.pdf", [root], 2) == str(root / "a")
    assert label_for(root / "d.pdf", [root], 2) == str(root)
    assert label_for(tmp_path / "fuera.pdf", [root], 2) is None


def test_dataset_filters_noise(dataset: Dataset, home: Path) -> None:
    labels = set(dataset.labels)
    docs = home / "Documentos"
    assert labels == {
        str(docs / "Finanzas/Banco"),
        str(docs / "Universidad/Calidad"),
        str(home / "Imágenes/Viajes"),
    }
    assert not any("proyecto" in label or "otro_repo" in label for label in labels)
    assert not any("Descargas" in label for label in labels)
    assert not any("Varios" in label for label in labels)  # menos de min_examples
    assert sorted(dataset.stats.project_dirs) == [str(docs / "otro_repo"), str(docs / "proyecto")]
    assert dataset.stats.in_projects == 41


def test_music_folders_per_artist_are_small_classes(dataset: Dataset, home: Path) -> None:
    # Cada «Música/Artista N» tiene un archivo: con profundidad 1 bajo Música son clases
    # distintas y ninguna llega al mínimo; con max_depth=0 se agrupan en «Música».
    assert not any("Artista" in label for label in dataset.labels)


def test_depth_zero_groups_by_root(db: Database, home: Path) -> None:
    for root in _roots(home):
        Scanner(db, ScanSettings(), mounts=()).scan(root)
    shallow = replace(SETTINGS, max_depth=0)
    ds = build_dataset(db, _roots(home), shallow, exclude=[home / "Descargas"])
    assert str(home / "Música") in ds.labels


# --- reglas y modelo ------------------------------------------------------------------


@pytest.fixture
def shallow_model(db: Database, home: Path) -> DestinationModel:
    for root in _roots(home):
        Scanner(db, ScanSettings(), mounts=()).scan(root)
    shallow = replace(SETTINGS, max_depth=0)  # una carpeta por raíz: Música, Imágenes…
    ds = build_dataset(db, _roots(home), shallow, exclude=[home / "Descargas"])
    return DestinationModel.train(ds, shallow)


def test_rule_learned_for_extension(shallow_model: DestinationModel, home: Path) -> None:
    rule = shallow_model.rules["ext:mp3"]
    assert rule.label == str(home / "Música")
    assert rule.support == rule.hits == 12


def test_rules_in_conflict_do_not_decide(shallow_model: DestinationModel) -> None:
    rules = dict(shallow_model.rules)
    rules["ext:jpg"] = replace(rules["ext:jpg"])
    assert apply_rules(rules, ["ext:mp3", "ext:jpg"]) is None
    assert apply_rules(rules, ["ext:mp3"]) is not None


def test_predicts_known_habits(model: DestinationModel, home: Path, tmp_path: Path) -> None:
    new = tmp_path / "nuevos"
    assert _predict(model, _write(new / "cartola_mensual_2025-03.pdf")) == str(
        home / "Documentos/Finanzas/Banco"
    )
    assert _predict(model, _write(new / "pruebas unitarias clase 13.pdf")) == str(
        home / "Documentos/Universidad/Calidad"
    )
    assert _predict(model, _write(new / "IMG_20250102_101010.jpg")) == str(home / "Imágenes/Viajes")


def test_mp3_goes_to_music_by_rule(
    shallow_model: DestinationModel, home: Path, tmp_path: Path
) -> None:
    prediction = shallow_model.predict(extract_features(_write(tmp_path / "nueva.mp3")))
    assert prediction.source == "regla"
    assert prediction.best is not None and prediction.best.label == str(home / "Música")
    assert prediction.rule is not None and prediction.rule.token == "ext:mp3"


def test_top3_probabilities_are_ordered_and_bounded(
    model: DestinationModel, tmp_path: Path
) -> None:
    prediction = model.predict(extract_features(_write(tmp_path / "cartola_2025-01.pdf")))
    probs = [s.probability for s in prediction.suggestions]
    assert len(probs) == 3
    assert probs == sorted(probs, reverse=True)
    assert all(0 <= p <= 1 for p in probs)
    assert sum(probs) <= 1 + 1e-9


def test_abstains_when_unsure(dataset: Dataset, tmp_path: Path) -> None:
    # Con un umbral inalcanzable el modelo siempre se abstiene.
    strict = DestinationModel.train(dataset, replace(SETTINGS, abstain_below=1.01))
    prediction = strict.predict(extract_features(_write(tmp_path / "zzqx.bin")))
    assert prediction.abstained and prediction.best is None
    assert prediction.suggestions  # las alternativas siguen disponibles para la GUI


def test_explanation_uses_real_features(model: DestinationModel, tmp_path: Path) -> None:
    features = extract_features(_write(tmp_path / "cartola_mensual_2025-03.pdf"))
    reasons = model.predict(features).reasons
    assert reasons
    real = set(features.tokens()) | {"nombre"}
    assert all(token in real for token, weight in reasons)
    assert all(weight > 0 for _, weight in reasons)
    assert "pal:cartola" in {t for t, _ in reasons} or "nombre" in {t for t, _ in reasons}


def test_requires_two_classes(db: Database, tmp_path: Path) -> None:
    for i in range(5):
        _write(tmp_path / f"solo/A/{i}.txt")
    Scanner(db, ScanSettings(), mounts=()).scan(tmp_path / "solo")
    ds = build_dataset(db, [tmp_path / "solo"], SETTINGS)
    with pytest.raises(NotEnoughDataError):
        DestinationModel.train(ds, SETTINGS)


# --- feedback ---------------------------------------------------------------------------


def test_repeated_corrections_change_prediction(
    model: DestinationModel, home: Path, tmp_path: Path
) -> None:
    # Fotos de documentos escaneados que el usuario quiere en Banco, no en Viajes.
    target = str(home / "Documentos/Finanzas/Banco")
    probe = extract_features(_write(tmp_path / "IMG_20250301_090000.jpg"))
    assert model.predict(probe).suggestions[0].label != target
    for i in range(8):
        sample = extract_features(_write(tmp_path / f"IMG_2025030{i}_0900{i:02d}.jpg"))
        assert model.learn(sample, target)
        if model.predict(probe).suggestions[0].label == target:
            break
    assert model.predict(probe).suggestions[0].label == target


def test_new_folder_requires_retraining(
    db: Database, home: Path, model: DestinationModel, tmp_path: Path
) -> None:
    new_label = str(home / "Documentos/Salud")
    sample = extract_features(_write(tmp_path / "receta medica dr perez.pdf"))
    assert not model.learn(sample, new_label)

    with db.session() as s:
        for i in range(4):
            features = extract_features(_write(tmp_path / f"receta medica {i}.pdf", bytes([i])))
            s.add(
                Feedback(
                    file_path=str(tmp_path / f"receta medica {i}.pdf"),
                    suggested=None,
                    chosen=new_label,
                    accepted=False,
                    features=features.to_json(),
                )
            )
    retrained = DestinationModel.train(
        build_dataset(db, _roots(home), SETTINGS, exclude=[home / "Descargas"]), SETTINGS
    )
    assert new_label in retrained.classes
    assert _predict(retrained, _write(tmp_path / "receta medica dr gomez.pdf")) == new_label


def test_correction_weakens_contradicted_rule(
    shallow_model: DestinationModel, home: Path, tmp_path: Path
) -> None:
    other = str(home / "Documentos")
    for i in range(3):
        shallow_model.learn(extract_features(_write(tmp_path / f"podcast {i}.mp3")), other)
    assert "ext:mp3" not in shallow_model.rules  # 12 de 15 ya no llega al 90 %


# --- persistencia ------------------------------------------------------------------------


def test_save_load_gives_same_predictions(model: DestinationModel, tmp_path: Path) -> None:
    store = ModelStore(tmp_path / "models")
    version = store.save(model)
    loaded = store.load()
    probe = [extract_features(_write(tmp_path / n)) for n in ("cartola_2025-09.pdf", "x.jpg")]
    assert version == 1
    assert loaded.predict_many(probe) == model.predict_many(probe)


def test_versions_are_pruned_and_revertible(model: DestinationModel, tmp_path: Path) -> None:
    store = ModelStore(tmp_path / "models", keep=2)
    for _ in range(4):
        store.save(model)
    assert [i.version for i in store.list()] == [3, 4]
    assert store.current_version() == 4
    store.revert(3)
    assert store.current_version() == 3
    store.save(model)  # la 3 sigue siendo la más reciente tras la activa
    assert [i.version for i in store.list()] == [4, 5]
    with pytest.raises(ModelNotFoundError):
        store.revert(1)


def test_missing_and_incompatible_models(
    model: DestinationModel, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = ModelStore(tmp_path / "models")
    with pytest.raises(ModelNotFoundError):
        store.load()
    store.save(model)
    monkeypatch.setattr(store_module, "FEATURES_VERSION", 999)
    with pytest.raises(IncompatibleModelError):
        store.load()


# --- evaluación y privacidad ---------------------------------------------------------------


def test_evaluation_reports_metrics(dataset: Dataset) -> None:
    result = evaluate(dataset, SETTINGS)
    assert result.n_test > 0 and result.n_train > result.n_test
    assert 0 <= result.coverage <= 1
    assert 0 <= result.top1_accuracy <= result.top3_accuracy <= 1
    assert 0 <= result.ece <= 1
    assert sum(b.count for b in result.bins) == result.n_test


@pytest.fixture
def no_network(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Prohíbe sockets de red (los sockets Unix, como D-Bus, siguen permitidos)."""
    real = socket.socket

    class Guarded(real):  # type: ignore[misc,valid-type]
        def __init__(self, family: Any = socket.AF_INET, *args: Any, **kwargs: Any) -> None:
            if family in (socket.AF_INET, socket.AF_INET6):
                raise AssertionError("Conexión de red prohibida")
            super().__init__(family, *args, **kwargs)

    monkeypatch.setattr(socket, "socket", Guarded)
    yield


def test_no_data_leaves_the_machine(
    no_network: None, db: Database, home: Path, tmp_path: Path
) -> None:
    with pytest.raises(AssertionError, match="prohibida"):
        socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    socket.socket(socket.AF_UNIX, socket.SOCK_STREAM).close()

    for root in _roots(home):
        Scanner(db, ScanSettings(), mounts=()).scan(root)
    ds = build_dataset(db, _roots(home), SETTINGS, exclude=[home / "Descargas"])
    model = DestinationModel.train(ds, SETTINGS)
    store = ModelStore(tmp_path / "models")
    store.save(model)
    store.load().predict(extract_features(_write(tmp_path / "cartola_2025-01.pdf")))
    evaluate(ds, SETTINGS)
