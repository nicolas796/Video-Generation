"""Merge users and pipeline_state branches

Revision ID: 3b13b7e838b7
Revises: 84a789e4f86a, b5f6c7d8e9f0
Create Date: 2026-02-28 22:37:53.860103

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '3b13b7e838b7'
down_revision = ('84a789e4f86a', 'b5f6c7d8e9f0')
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    pass
