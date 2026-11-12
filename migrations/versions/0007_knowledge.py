"""Owner-confirmed knowledge documents and their retrieval chunks."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("knowledge_documents",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("owner_id", sa.BigInteger(), nullable=False),
        sa.Column("place_id", sa.String(40), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False, server_default=""),
        sa.Column("chunks", JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("digest", sa.String(64)),
        sa.Column("status", sa.String(12), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False))
    op.create_index("ix_knowledge_owner", "knowledge_documents", ["owner_id", "place_id", "status"])
    op.create_index("uq_knowledge_content", "knowledge_documents", ["owner_id", "place_id", "digest"],
                    unique=True, postgresql_where=sa.text("status = 'active'"))


def downgrade():
    op.drop_table("knowledge_documents")
