"""Add use case generation mode and clip strategy overrides

Revision ID: b8d9e0f1a2b3
Revises: e2f3a4b5c6d7
Create Date: 2026-03-06 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'b8d9e0f1a2b3'
down_revision = 'e2f3a4b5c6d7'
branch_labels = None
depends_on = None


def _column_exists(table_name, column_name):
    conn = op.get_bind()
    result = conn.execute(sa.text(
        "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
        "WHERE table_name = :t AND column_name = :c)"
    ), {"t": table_name, "c": column_name})
    return result.scalar()


def upgrade():
    if not _column_exists('use_cases', 'generation_mode'):
        op.add_column('use_cases', sa.Column('generation_mode', sa.String(length=50), nullable=True, server_default='balanced'))
    if not _column_exists('use_cases', 'clip_strategy_overrides'):
        op.add_column('use_cases', sa.Column('clip_strategy_overrides', sa.JSON(), nullable=True))

    # Backfill defaults for existing rows
    op.execute("UPDATE use_cases SET generation_mode = 'balanced' WHERE generation_mode IS NULL")
    op.execute("UPDATE use_cases SET clip_strategy_overrides = '{}'::json WHERE clip_strategy_overrides IS NULL")


def downgrade():
    if _column_exists('use_cases', 'clip_strategy_overrides'):
        op.drop_column('use_cases', 'clip_strategy_overrides')
    if _column_exists('use_cases', 'generation_mode'):
        op.drop_column('use_cases', 'generation_mode')
