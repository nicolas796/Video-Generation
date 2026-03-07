"""add hooks table

Revision ID: d85f66976387
Revises: f7a9c3e2b1d4
Create Date: 2026-03-06 18:20:28.995982

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


# revision identifiers, used by Alembic.
revision = 'd85f66976387'
down_revision = 'f7a9c3e2b1d4'
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = inspect(bind)
    if 'hooks' in inspector.get_table_names():
        return

    op.create_table(
        'hooks',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('use_case_id', sa.Integer(), nullable=False),
        sa.Column('hook_type', sa.String(length=50), nullable=False),
        sa.Column('winning_variant_index', sa.Integer(), nullable=True),
        sa.Column('variants', sa.JSON(), nullable=True),
        sa.Column('image_paths', sa.JSON(), nullable=True),
        sa.Column('audio_path', sa.String(length=500), nullable=True),
        sa.Column('video_path', sa.String(length=500), nullable=True),
        sa.Column('status', sa.String(length=20), nullable=False, server_default='draft'),
        sa.Column('error_message', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['use_case_id'], ['use_cases.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('use_case_id', name='uq_hooks_use_case_id')
    )


def downgrade():
    bind = op.get_bind()
    inspector = inspect(bind)
    if 'hooks' not in inspector.get_table_names():
        return
    op.drop_table('hooks')
