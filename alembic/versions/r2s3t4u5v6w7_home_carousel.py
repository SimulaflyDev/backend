"""Admin-managed home carousel and public marketing images.

Revision ID: r2s3t4u5v6w7
Revises: q1r2s3t4u5v6
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "r2s3t4u5v6w7"
down_revision = "q1r2s3t4u5v6"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("home_carousel",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("settings", postgresql.JSONB(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("id = 1", name="ck_home_carousel_singleton"))
    op.create_table("home_carousel_images",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("data", sa.LargeBinary(), nullable=False),
        sa.Column("media_type", sa.String(40), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False))
    # Existing deployments skip initial RBAC seeding, so add the new catalog
    # entries and predefined Org Manager grants as part of the rollout.
    op.execute("""
        INSERT INTO admin_permissions (id, key, description) VALUES
          ('3474bc98-d44a-4693-956d-611e8354a3ef', 'content.read', 'View app home content'),
          ('4421f5c6-cc63-48e5-85cb-e253b9203af9', 'content.manage', 'Manage app home content')
        ON CONFLICT (key) DO NOTHING
    """)
    op.execute("""
        INSERT INTO role_permissions (role_id, permission_id)
        SELECT r.id, p.id FROM admin_roles r CROSS JOIN admin_permissions p
        WHERE r.name = 'Org Manager' AND r.is_predefined = true
          AND p.key IN ('content.read', 'content.manage')
        ON CONFLICT (role_id, permission_id) DO NOTHING
    """)


def downgrade():
    # Retain permission catalog/grants on rollback to avoid erasing custom
    # administrator assignments; the rolled-back API has no content routes.
    op.drop_table("home_carousel_images")
    op.drop_table("home_carousel")
