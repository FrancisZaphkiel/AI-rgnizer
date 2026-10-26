"""Motor SQLite, sesiones y migraciones."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from importlib.resources import files
from pathlib import Path
from typing import Any

from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

MIGRATIONS_PACKAGE = "airgnizer.storage.migrations"


def _set_sqlite_pragmas(dbapi_connection: Any, _record: Any) -> None:
    cursor = dbapi_connection.cursor()
    try:
        # WAL: lectores (GUI) concurrentes con un único escritor (daemon).
        cursor.execute("PRAGMA journal_mode=WAL")
        # FULL: el journal de acciones debe sobrevivir a un corte de energía.
        cursor.execute("PRAGMA synchronous=FULL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=5000")
    finally:
        cursor.close()


def create_db_engine(db_path: Path | None) -> Engine:
    """Crea el motor; ``None`` usa una base en memoria (tests)."""
    if db_path is None:
        url = "sqlite+pysqlite:///:memory:"
    else:
        db_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        url = f"sqlite+pysqlite:///{db_path}"
    engine = create_engine(url)
    event.listen(engine, "connect", _set_sqlite_pragmas)
    return engine


def alembic_config(engine: Engine) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(files(MIGRATIONS_PACKAGE)))
    cfg.attributes["engine"] = engine
    return cfg


def upgrade(engine: Engine, revision: str = "head") -> None:
    command.upgrade(alembic_config(engine), revision)


def downgrade(engine: Engine, revision: str) -> None:
    command.downgrade(alembic_config(engine), revision)


class Database:
    """Punto de acceso único a la base de datos."""

    def __init__(self, db_path: Path | None) -> None:
        self.engine = create_db_engine(db_path)
        self._sessions = sessionmaker(self.engine, expire_on_commit=False)

    def migrate(self) -> None:
        upgrade(self.engine)

    def new_session(self) -> Session:
        """Sesión sin transacción automática, para procesos por lotes que confirman a mano."""
        return self._sessions()

    @contextmanager
    def session(self) -> Iterator[Session]:
        """Sesión transaccional: commit al salir, rollback ante excepción."""
        with self._sessions() as session, session.begin():
            yield session

    def dispose(self) -> None:
        self.engine.dispose()
