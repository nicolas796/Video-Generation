"""Add hook preview tracking columns

Revision ID: 0c5f4b6a1d3e
Revises: 3b13b7e838b7
Create Date: 2026-03-07 23:00:08.534944

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '0c5f4b6a1d3e'
down_revision = '3b13b7e838b7'
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    table_names = inspector.get_table_names()
    if 'hooks' not in table_names:
        return

    existing_columns = {col['name'] for col in inspector.get_columns('hooks')}

    if 'preview_progress' not in existing_columns:
        op.add_column('hooks', sa.Column('preview_progress', sa.Integer(), nullable=False, server_default='0'))
        op.alter_column('hooks', 'preview_progress', server_default=None)
        op.execute("UPDATE hooks SET preview_progress = 0 WHERE preview_progress IS NULL")

    if 'preview_assets' not in existing_columns:
        op.add_column('hooks', sa.Column('preview_assets', sa.JSON(), nullable=True))
        op.execute("UPDATE hooks SET preview_assets = '{}' WHERE preview_assets IS NULL")

    if 'preview_status_message' not in existing_columns:
        op.add_column('hooks', sa.Column('preview_status_message', sa.String(length=255), nullable=True))


def downgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    table_names = inspector.get_table_names()
    if 'hooks' not in table_names:
        return

    existing_columns = {col['name'] for col in inspector.get_columns('hooks')}

    if 'preview_status_message' in existing_columns:
        op.drop_column('hooks', 'preview_status_message')
    if 'preview_assets' in existing_columns:
        op.drop_column('hooks', 'preview_assets')
    if 'preview_progress' in existing_columns:
        op.drop_column('hooks', 'preview_progress')
