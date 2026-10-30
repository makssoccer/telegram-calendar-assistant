"""Persist explicitly confirmed invitation recipients."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("event_drafts", sa.Column("attendees", JSONB(), nullable=False,
                                          server_default=sa.text("'[]'::jsonb")))


def downgrade():
    op.drop_column("event_drafts", "attendees")
