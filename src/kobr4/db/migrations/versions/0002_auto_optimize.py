"""Optimisation automatique hebdomadaire des bots

Révision : 0002
Précédente : 0001
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import kobr4.db.models

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("bots") as batch:
        batch.add_column(
            sa.Column("auto_optimize", sa.Boolean(), nullable=False, server_default=sa.false())
        )
        batch.add_column(
            sa.Column(
                "last_auto_optimize_at", kobr4.db.models.UtcDateTime(timezone=True), nullable=True
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("bots") as batch:
        batch.drop_column("last_auto_optimize_at")
        batch.drop_column("auto_optimize")
