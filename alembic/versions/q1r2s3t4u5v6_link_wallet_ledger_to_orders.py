"""link wallet ledger deductions to orders for invoice traceability

Revision ID: q1r2s3t4u5v6
Revises: p1q2r3s4t5u6
Create Date: 2026-10-01
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "q1r2s3t4u5v6"
down_revision: Union[str, Sequence[str], None] = "p1q2r3s4t5u6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "ledger_entries",
        sa.Column(
            "related_order_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("orders.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_ledger_entries_related_order_id",
        "ledger_entries",
        ["related_order_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_ledger_entries_related_order_id", table_name="ledger_entries")
    op.drop_column("ledger_entries", "related_order_id")
