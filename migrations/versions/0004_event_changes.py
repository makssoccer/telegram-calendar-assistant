"""Store immutable move/delete confirmations."""
from alembic import op
import sqlalchemy as sa

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("event_drafts", sa.Column("operation", sa.String(10), nullable=False, server_default="create"))
    op.add_column("event_drafts", sa.Column("target_event_id", sa.Text()))
    op.add_column("event_drafts", sa.Column("target_etag", sa.Text()))
    op.add_column("event_drafts", sa.Column("original_start", sa.DateTime(timezone=True)))
    op.add_column("event_drafts", sa.Column("original_end", sa.DateTime(timezone=True)))


def downgrade():
    for name in ("original_end", "original_start", "target_etag", "target_event_id", "operation"):
        op.drop_column("event_drafts", name)
