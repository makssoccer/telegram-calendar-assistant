"""Telegram update receipts and model quota reservations."""
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""CREATE TABLE telegram_receipts (bot bigint NOT NULL, update_id bigint NOT NULL,
               seen timestamptz NOT NULL DEFAULT now(), PRIMARY KEY(bot, update_id))""")
    op.execute("CREATE INDEX ix_receipts_seen ON telegram_receipts(seen)")
    op.execute("""CREATE TABLE model_reservations (owner bigint NOT NULL, units integer NOT NULL,
               sent timestamptz NOT NULL DEFAULT now())""")
    op.execute("CREATE INDEX ix_reservations_sent ON model_reservations(sent)")


def downgrade():
    op.drop_table("model_reservations")
    op.drop_table("telegram_receipts")
