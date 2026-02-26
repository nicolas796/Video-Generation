"""Add pipeline state column

Revision ID: 84a789e4f86a
Revises: a24ab76fff9e
Create Date: 2026-02-26 10:29:24.041283

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '84a789e4f86a'
down_revision = 'a24ab76fff9e'
branch_labels = None
depends_on = None


def upgrade():
    """Add pipeline_state column to use_cases."""
    with op.batch_alter_table('use_cases', schema=None) as batch_op:
        batch_op.add_column(sa.Column('pipeline_state', sa.JSON(), nullable=True))


def downgrade():
    """Remove pipeline_state column."""
    with op.batch_alter_table('use_cases', schema=None) as batch_op:
        batch_op.drop_column('pipeline_state')
