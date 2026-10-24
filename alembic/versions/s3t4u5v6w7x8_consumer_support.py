"""Customer support threads, idempotency and admin-managed help content.

Revision ID: s3t4u5v6w7x8
Revises: r2s3t4u5v6w7
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "s3t4u5v6w7x8"
down_revision = "r2s3t4u5v6w7"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("support_tickets", sa.Column("client_request_id", sa.String(64), nullable=True))
    op.add_column("support_tickets", sa.Column("lead_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.add_column("support_tickets", sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.create_foreign_key("fk_support_lead", "support_tickets", "buyer_leads", ["lead_id"], ["id"], ondelete="SET NULL")
    op.create_foreign_key("fk_support_session", "support_tickets", "design_sessions", ["session_id"], ["id"], ondelete="SET NULL")
    op.create_unique_constraint("uq_support_request", "support_tickets", ["requester_type", "requester_id", "client_request_id"])
    op.add_column("support_messages", sa.Column("client_message_id", sa.String(64), nullable=True))
    op.create_unique_constraint("uq_support_message_request", "support_messages", ["ticket_id", "client_message_id"])
    op.create_table("support_settings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("settings", postgresql.JSONB(), nullable=False),
        sa.CheckConstraint("id = 1", name="ck_support_settings_singleton"))


def downgrade():
    op.drop_table("support_settings")
    op.drop_constraint("uq_support_message_request", "support_messages", type_="unique")
    op.drop_column("support_messages", "client_message_id")
    op.drop_constraint("uq_support_request", "support_tickets", type_="unique")
    op.drop_constraint("fk_support_session", "support_tickets", type_="foreignkey")
    op.drop_constraint("fk_support_lead", "support_tickets", type_="foreignkey")
    for column in ("session_id", "lead_id", "client_request_id"):
        op.drop_column("support_tickets", column)
