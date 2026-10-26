from __future__ import annotations

import os
from pathlib import Path

import pytest

from airgnizer.app import AppContext, load_model
from airgnizer.learning.store import ModelStore


def test_active_model_is_loaded_once_until_it_changes(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = AppContext.open()
    model_file = isolated_home / "model-0001.joblib"
    model_file.write_bytes(b"x")
    loads: list[object] = []

    def load(self: ModelStore, version: int | None = None) -> object:
        loads.append(object())
        return loads[-1]

    monkeypatch.setattr(ModelStore, "current_version", lambda self: 1)
    monkeypatch.setattr(ModelStore, "path_of", lambda self, version: model_file)
    monkeypatch.setattr(ModelStore, "load", load)

    first = load_model(ctx)
    assert load_model(ctx) is first  # aceptar 50 descargas no relee el modelo 50 veces
    assert len(loads) == 1

    os.utime(model_file, ns=(0, 1))  # «train» guardó otro
    assert load_model(ctx) is not first
    assert len(loads) == 2
    ctx.db.dispose()
