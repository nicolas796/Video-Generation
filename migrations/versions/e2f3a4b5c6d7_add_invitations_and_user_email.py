"""Add brand_invitations table and email column on users

Revision ID: e2f3a4b5c6d7
Revises: d1e2f3a4b5c6
Create Date: 2026-03-05 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'e2f3a4b5c6d7'
down_revision = 'd1e2f3a4b5c6'
branch_labels = None
depends_on = None


def upgrade():
    # Add email column to users (nullable, unique)
    op.add_column('users', sa.Column('email', sa.String(255), nullable=True))
    op.create_index('ix_users_email', 'users', ['email'], unique=True)

    # Create brand_invitations table
    op.create_table(
        'brand_invitations',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('brand_id', sa.Integer(), sa.ForeignKey('brands.id'), nullable=False),
        sa.Column('email', sa.String(255), nullable=False),
        sa.Column('role', sa.String(50), server_default='member'),
        sa.Column('token', sa.String(128), unique=True, nullable=False),
        sa.Column('invited_by_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('status', sa.String(50), server_default='pending'),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now()),
        sa.Column('expires_at', sa.DateTime(), nullable=True),
        sa.Column('accepted_at', sa.DateTime(), nullable=True),
    )
    op.create_index('ix_brand_invitations_token', 'brand_invitations', ['token'], unique=True)
    op.create_index('ix_brand_invitations_email_brand', 'brand_invitations', ['email', 'brand_id'])


def downgrade():
    op.drop_table('brand_invitations')
    op.drop_index('ix_users_email', table_name='users')
    op.drop_column('users', 'email')
