"""Persist reminder preferences and delivery attempts."""
from alembic import op
import sqlalchemy as sa

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("reminder_settings",
        sa.Column("owner_id", sa.BigInteger(), primary_key=True),
        sa.Column("enabled", sa.Boolean(), nullable=False))
    op.create_table("reminder_deliveries",
        sa.Column("owner_id", sa.BigInteger(), primary_key=True),
        sa.Column("calendar_id", sa.Text(), primary_key=True),
        sa.Column("event_id", sa.Text(), primary_key=True),
        sa.Column("starts_at", sa.DateTime(timezone=True), primary_key=True),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("attempted_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()))


def downgrade():
    op.drop_table("reminder_deliveries")
    op.drop_table("reminder_settings")
