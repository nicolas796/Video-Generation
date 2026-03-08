"""Merge all migration branches

Revision ID: 4d42897f3386
Revises: 0c5f4b6a1d3e, c9f0a1b2c3d4, d85f66976387
Create Date: 2026-03-07 19:34:06.679443

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '4d42897f3386'
down_revision = ('0c5f4b6a1d3e', 'c9f0a1b2c3d4', 'd85f66976387')
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    pass
