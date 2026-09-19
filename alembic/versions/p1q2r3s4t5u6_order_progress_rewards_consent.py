"""Order delivery/payment lifecycle, seller snapshots, and new-account consent.

Revision ID: p1q2r3s4t5u6
Revises: o1p2q3r4s5t6
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "p1q2r3s4t5u6"
down_revision = "o1p2q3r4s5t6"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("orders", sa.Column("merchant_snapshot", postgresql.JSONB(), nullable=False, server_default="{}"))
    # Existing payments are unknown; only NEW orders start as pending.
    op.add_column("orders", sa.Column("payment_status", sa.String(16), nullable=False, server_default="unknown"))
    op.alter_column("orders", "payment_status", server_default="pending")
    for name in ("payment_completed_at", "shipped_at", "out_for_delivery_at", "delivered_at", "reward_granted_at"):
        op.add_column("orders", sa.Column(name, sa.DateTime(timezone=True), nullable=True))
    op.add_column("orders", sa.Column("reward_tokens", sa.Integer(), nullable=False, server_default="0"))
    # Do not update any existing consent choice or mint rewards on old orders.
    op.alter_column("users", "model_improvement_consent", existing_type=sa.Boolean(), server_default=sa.true())


def downgrade():
    op.alter_column("users", "model_improvement_consent", existing_type=sa.Boolean(), server_default=sa.false())
    for name in ("reward_tokens", "reward_granted_at", "delivered_at", "out_for_delivery_at", "shipped_at", "payment_completed_at", "payment_status", "merchant_snapshot"):
        op.drop_column("orders", name)
