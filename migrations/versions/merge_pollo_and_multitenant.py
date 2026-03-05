"""Merge pollo_video_url and multi_tenant branches

Revision ID: d1e2f3a4b5c6
Revises: f7a9c3e2b1d4, c1d2e3f4a5b6
Create Date: 2026-03-05 01:15:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'd1e2f3a4b5c6'
down_revision = ('f7a9c3e2b1d4', 'c1d2e3f4a5b6')
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    pass
