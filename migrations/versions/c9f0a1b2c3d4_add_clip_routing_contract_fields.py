"""Add clip routing contract fields for assembly handoff

Revision ID: c9f0a1b2c3d4
Revises: b8d9e0f1a2b3
Create Date: 2026-03-06 00:00:01.000000

"""
from alembic import op
import sqlalchemy as sa


revision = 'c9f0a1b2c3d4'
down_revision = 'b8d9e0f1a2b3'
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
    if not _column_exists('video_clips', 'generation_strategy'):
        op.add_column('video_clips', sa.Column('generation_strategy', sa.String(length=50), nullable=True, server_default='composite_then_kling'))
    if not _column_exists('video_clips', 'asset_source'):
        op.add_column('video_clips', sa.Column('asset_source', sa.String(length=50), nullable=True, server_default='product_image'))
    if not _column_exists('video_clips', 'script_segment_ref'):
        op.add_column('video_clips', sa.Column('script_segment_ref', sa.Text(), nullable=True))
    if not _column_exists('video_clips', 'quality_score'):
        op.add_column('video_clips', sa.Column('quality_score', sa.Float(), nullable=True))

    op.execute("UPDATE video_clips SET generation_strategy = COALESCE(generation_strategy, 'composite_then_kling')")
    op.execute("UPDATE video_clips SET asset_source = COALESCE(asset_source, 'product_image')")


def downgrade():
    if _column_exists('video_clips', 'quality_score'):
        op.drop_column('video_clips', 'quality_score')
    if _column_exists('video_clips', 'script_segment_ref'):
        op.drop_column('video_clips', 'script_segment_ref')
    if _column_exists('video_clips', 'asset_source'):
        op.drop_column('video_clips', 'asset_source')
    if _column_exists('video_clips', 'generation_strategy'):
        op.drop_column('video_clips', 'generation_strategy')
