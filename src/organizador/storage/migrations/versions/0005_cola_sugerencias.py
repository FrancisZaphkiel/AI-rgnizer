"""cola de sugerencias de las carpetas vigiladas

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-26 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "suggestions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("size", sa.BigInteger(), nullable=False),
        sa.Column("mtime_ns", sa.BigInteger(), nullable=False),
        sa.Column("inode", sa.BigInteger(), nullable=False),
        sa.Column("device", sa.BigInteger(), nullable=False),
        sa.Column("destination", sa.Text(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("source", sa.String(length=16), nullable=True),
        sa.Column("alternatives", sa.Text(), nullable=False),
        sa.Column(
            "state",
            sa.Enum(
                "pending",
                "accepted",
                "auto",
                "ignored",
                "gone",
                name="suggestionstate",
                native_enum=False,
                length=16,
            ),
            nullable=False,
        ),
        sa.Column("chosen", sa.Text(), nullable=True),
        sa.Column("action_id", sa.String(length=36), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["action_id"],
            ["actions_journal.id"],
            name=op.f("fk_suggestions_action_id_actions_journal"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_suggestions")),
        sa.UniqueConstraint("path", "size", "mtime_ns", "inode", name=op.f("uq_suggestions_path")),
    )
    op.create_index("ix_suggestions_state", "suggestions", ["state"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_suggestions_state", table_name="suggestions")
    op.drop_table("suggestions")
