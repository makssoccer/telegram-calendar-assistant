"""Persist bounded conversation context."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("conversations",
        sa.Column("owner_id", sa.BigInteger(), primary_key=True),
        sa.Column("messages", JSONB(), nullable=False))


def downgrade():
    op.drop_table("conversations")
