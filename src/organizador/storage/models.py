"""Esquema de la base de datos (SQLAlchemy 2, tipado declarativo).

Cualquier cambio aquí requiere una nueva revisión de Alembic en ``migrations/versions``.
"""

from __future__ import annotations

import enum
import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

# Nombres de restricciones deterministas: necesarios para migraciones en SQLite (batch mode).
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class Directory(Base):
    __tablename__ = "directories"

    id: Mapped[int] = mapped_column(primary_key=True)
    path: Mapped[str] = mapped_column(Text, unique=True)
    # Raíz indexada a la que pertenece (para reconciliar solo lo escaneado).
    root: Mapped[str] = mapped_column(Text, index=True)
    mtime_ns: Mapped[int] = mapped_column(BigInteger)
    last_seen_scan: Mapped[int] = mapped_column(Integer, index=True)

    files: Mapped[list[File]] = relationship(
        back_populates="directory", cascade="all, delete-orphan", passive_deletes=True
    )


class File(Base):
    __tablename__ = "files"
    __table_args__ = (
        UniqueConstraint("directory_id", "name"),
        Index("ix_files_size", "size"),
        Index("ix_files_device_inode", "device", "inode"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    directory_id: Mapped[int] = mapped_column(
        ForeignKey("directories.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(Text)
    size: Mapped[int] = mapped_column(BigInteger)
    mtime_ns: Mapped[int] = mapped_column(BigInteger)
    ctime_ns: Mapped[int] = mapped_column(BigInteger)
    inode: Mapped[int] = mapped_column(BigInteger)
    device: Mapped[int] = mapped_column(BigInteger)
    nlink: Mapped[int] = mapped_column(Integer, default=1)
    mime: Mapped[str | None] = mapped_column(String(255))
    # Hashes de contenido; se invalidan (None) ante cualquier cambio de metadatos.
    partial_hash: Mapped[str | None] = mapped_column(String(64))
    content_hash: Mapped[str | None] = mapped_column(String(64), index=True)
    last_seen_scan: Mapped[int] = mapped_column(Integer, index=True)

    directory: Mapped[Directory] = relationship(back_populates="files")


class ScanRun(Base):
    __tablename__ = "scan_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    root: Mapped[str] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled: Mapped[bool] = mapped_column(default=False)
    files_seen: Mapped[int] = mapped_column(Integer, default=0)
    files_changed: Mapped[int] = mapped_column(Integer, default=0)
    files_removed: Mapped[int] = mapped_column(Integer, default=0)
    errors: Mapped[int] = mapped_column(Integer, default=0)


class ScanError(Base):
    __tablename__ = "scan_errors"

    id: Mapped[int] = mapped_column(primary_key=True)
    scan_id: Mapped[int] = mapped_column(ForeignKey("scan_runs.id", ondelete="CASCADE"), index=True)
    path: Mapped[str] = mapped_column(Text)
    errno: Mapped[int | None] = mapped_column(Integer)
    message: Mapped[str] = mapped_column(Text)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ActionKind(enum.StrEnum):
    MOVE = "move"
    RENAME = "rename"
    TRASH = "trash"
    # Reversión de otra acción; ``reverts_id`` apunta a la original.
    UNDO = "undo"


class ActionState(enum.StrEnum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    FAILED = "failed"
    REVERTED = "reverted"


ACTIVE_STATES = (ActionState.PENDING, ActionState.IN_PROGRESS)


def _str_enum(enum_cls: type[enum.StrEnum]) -> Enum:
    # Guarda el valor ("move"), no el nombre del miembro ("MOVE").
    return Enum(
        enum_cls,
        native_enum=False,
        length=16,
        values_callable=lambda members: [m.value for m in members],
    )


class ActionJournal(Base):
    """Registro de cada operación sobre archivos.

    Se escribe (y se confirma) **antes** de tocar el disco; ver ``organizador.actions``.
    """

    __tablename__ = "actions_journal"
    __table_args__ = (Index("ix_actions_journal_state", "state"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    kind: Mapped[ActionKind] = mapped_column(_str_enum(ActionKind))
    state: Mapped[ActionState] = mapped_column(_str_enum(ActionState), default=ActionState.PENDING)
    source: Mapped[str] = mapped_column(Text)
    destination: Mapped[str | None] = mapped_column(Text)
    # Ruta temporal usada en movimientos entre sistemas de archivos.
    temp_path: Mapped[str | None] = mapped_column(Text)
    # Huella del archivo en el momento de la acción.
    size: Mapped[int] = mapped_column(BigInteger)
    mtime_ns: Mapped[int] = mapped_column(BigInteger)
    inode: Mapped[int] = mapped_column(BigInteger)
    device: Mapped[int] = mapped_column(BigInteger)
    content_hash: Mapped[str | None] = mapped_column(String(64))
    reverts_id: Mapped[str | None] = mapped_column(ForeignKey("actions_journal.id"))
    batch_id: Mapped[str | None] = mapped_column(String(36), index=True)
    origin: Mapped[str] = mapped_column(String(16), default="cli")
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class Feedback(Base):
    """Respuesta del usuario a una sugerencia (Fase 3)."""

    __tablename__ = "feedback"

    id: Mapped[int] = mapped_column(primary_key=True)
    file_path: Mapped[str] = mapped_column(Text)
    suggested: Mapped[str | None] = mapped_column(Text)
    chosen: Mapped[str | None] = mapped_column(Text)
    accepted: Mapped[bool]
    confidence: Mapped[float | None] = mapped_column(Float)
    action_id: Mapped[str | None] = mapped_column(ForeignKey("actions_journal.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class NamingPattern(Base):
    """Plantilla de nombres inferida para una carpeta (Fase 3)."""

    __tablename__ = "patterns"
    __table_args__ = (UniqueConstraint("directory", "template"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    directory: Mapped[str] = mapped_column(Text)
    template: Mapped[str] = mapped_column(Text)
    support: Mapped[int] = mapped_column(Integer, default=0)
    enabled: Mapped[bool] = mapped_column(default=True)
    user_edited: Mapped[bool] = mapped_column(default=False)


class SettingKV(Base):
    """Estado interno clave-valor (la configuración de usuario vive en ``config.toml``)."""

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
