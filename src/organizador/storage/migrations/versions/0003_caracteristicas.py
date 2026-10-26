"""características de archivos para el motor de sugerencias

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-26 10:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("files", schema=None) as batch_op:
        batch_op.add_column(sa.Column("features", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("features_version", sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("files", schema=None) as batch_op:
        batch_op.drop_column("features_version")
        batch_op.drop_column("features")
