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
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    # Add email column to users (nullable, unique) when missing.
    user_columns = {column['name'] for column in inspector.get_columns('users')}
    if 'email' not in user_columns:
        op.add_column('users', sa.Column('email', sa.String(255), nullable=True))

    user_indexes = {index['name'] for index in inspector.get_indexes('users')}
    if 'ix_users_email' not in user_indexes:
        op.create_index('ix_users_email', 'users', ['email'], unique=True)

    # Create brand_invitations table and indexes when missing.
    tables = set(inspector.get_table_names())
    if 'brand_invitations' not in tables:
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

    invitation_indexes = {index['name'] for index in inspector.get_indexes('brand_invitations')}
    if 'ix_brand_invitations_token' not in invitation_indexes:
        op.create_index('ix_brand_invitations_token', 'brand_invitations', ['token'], unique=True)
    if 'ix_brand_invitations_email_brand' not in invitation_indexes:
        op.create_index('ix_brand_invitations_email_brand', 'brand_invitations', ['email', 'brand_id'])


def downgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    tables = set(inspector.get_table_names())
    if 'brand_invitations' in tables:
        op.drop_table('brand_invitations')

    user_indexes = {index['name'] for index in inspector.get_indexes('users')}
    if 'ix_users_email' in user_indexes:
        op.drop_index('ix_users_email', table_name='users')

    user_columns = {column['name'] for column in inspector.get_columns('users')}
    if 'email' in user_columns:
        op.drop_column('users', 'email')
