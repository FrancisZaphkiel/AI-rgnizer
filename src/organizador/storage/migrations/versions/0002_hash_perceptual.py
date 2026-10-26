"""hash perceptual y resolución de imágenes

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-25 21:30:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("files", schema=None) as batch_op:
        batch_op.add_column(sa.Column("perceptual_hash", sa.String(length=16), nullable=True))
        batch_op.add_column(sa.Column("image_width", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("image_height", sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("files", schema=None) as batch_op:
        batch_op.drop_column("image_height")
        batch_op.drop_column("image_width")
        batch_op.drop_column("perceptual_hash")
