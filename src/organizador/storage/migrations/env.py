"""Entorno de Alembic: recibe el motor desde ``organizador.storage.db.alembic_config``."""

from __future__ import annotations

from alembic import context
from sqlalchemy import Engine

from organizador.storage.models import Base

config = context.config
engine = config.attributes.get("engine")
if not isinstance(engine, Engine):  # pragma: no cover - uso incorrecto
    raise RuntimeError("Usa organizador.storage.db.upgrade(); falta el motor en la configuración.")

with engine.begin() as connection:
    context.configure(
        connection=connection,
        target_metadata=Base.metadata,
        render_as_batch=True,  # SQLite no soporta ALTER TABLE completo
    )
    with context.begin_transaction():
        context.run_migrations()
