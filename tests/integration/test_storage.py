from __future__ import annotations

import pytest
from sqlalchemy import inspect, select, text

from organizador.storage import Database
from organizador.storage.db import downgrade, upgrade
from organizador.storage.models import ActionJournal, ActionKind, ActionState, Base

pytestmark = pytest.mark.integration


def test_migration_creates_all_tables(db: Database) -> None:
    tables = set(inspect(db.engine).get_table_names())
    assert set(Base.metadata.tables) <= tables


def test_pragmas_applied(db: Database) -> None:
    with db.engine.connect() as conn:
        assert conn.execute(text("PRAGMA journal_mode")).scalar() == "wal"
        assert conn.execute(text("PRAGMA foreign_keys")).scalar() == 1


def test_migrations_down_and_up_preserve_schema(db: Database) -> None:
    before = set(inspect(db.engine).get_table_names())
    downgrade(db.engine, "base")
    assert set(inspect(db.engine).get_table_names()) <= {"alembic_version"}
    upgrade(db.engine)
    assert set(inspect(db.engine).get_table_names()) == before


def test_enum_stored_as_value(db: Database) -> None:
    with db.session() as s:
        s.add(
            ActionJournal(kind=ActionKind.MOVE, source="/a", size=1, mtime_ns=1, inode=1, device=1)
        )
    with db.engine.connect() as conn:
        assert conn.execute(text("SELECT kind, state FROM actions_journal")).one() == (
            "move",
            "pending",
        )
    with db.session() as s:
        entry = s.scalars(select(ActionJournal)).one()
        assert entry.state is ActionState.PENDING


def test_session_rolls_back_on_error(db: Database) -> None:
    with pytest.raises(RuntimeError), db.session() as s:
        s.add(
            ActionJournal(kind=ActionKind.MOVE, source="/a", size=1, mtime_ns=1, inode=1, device=1)
        )
        s.flush()
        raise RuntimeError
    with db.session() as s:
        assert s.scalars(select(ActionJournal)).first() is None


def test_models_match_migrations(db: Database) -> None:
    """Todo cambio en los modelos debe venir con su revisión de Alembic."""
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext

    with db.engine.connect() as conn:
        assert compare_metadata(MigrationContext.configure(conn), Base.metadata) == []
