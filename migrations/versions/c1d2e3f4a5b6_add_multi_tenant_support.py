"""Add multi-tenant support (brands, memberships, usage tracking)

Revision ID: c1d2e3f4a5b6
Revises: 3b13b7e838b7
Create Date: 2026-03-04 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'c1d2e3f4a5b6'
down_revision = '3b13b7e838b7'
branch_labels = None
depends_on = None


def _table_exists(table_name):
    """Check if a table already exists in the database."""
    conn = op.get_bind()
    result = conn.execute(sa.text(
        "SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = :t)"
    ), {"t": table_name})
    return result.scalar()


def _column_exists(table_name, column_name):
    """Check if a column already exists in a table."""
    conn = op.get_bind()
    result = conn.execute(sa.text(
        "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
        "WHERE table_name = :t AND column_name = :c)"
    ), {"t": table_name, "c": column_name})
    return result.scalar()


def _index_exists(index_name):
    """Check if an index already exists."""
    conn = op.get_bind()
    result = conn.execute(sa.text(
        "SELECT EXISTS (SELECT 1 FROM pg_indexes WHERE indexname = :i)"
    ), {"i": index_name})
    return result.scalar()


def _fk_exists(constraint_name):
    """Check if a foreign key constraint already exists."""
    conn = op.get_bind()
    result = conn.execute(sa.text(
        "SELECT EXISTS (SELECT 1 FROM information_schema.table_constraints "
        "WHERE constraint_name = :c AND constraint_type = 'FOREIGN KEY')"
    ), {"c": constraint_name})
    return result.scalar()


def upgrade():
    # 1. Create brands table
    if not _table_exists('brands'):
        op.create_table('brands',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('name', sa.String(length=150), nullable=False),
            sa.Column('slug', sa.String(length=150), nullable=False),
            sa.Column('pollo_api_key', sa.String(length=500), nullable=True),
            sa.Column('elevenlabs_api_key', sa.String(length=500), nullable=True),
            sa.Column('openai_api_key', sa.String(length=500), nullable=True),
            sa.Column('settings', sa.JSON(), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=True),
            sa.Column('updated_at', sa.DateTime(), nullable=True),
            sa.PrimaryKeyConstraint('id'),
            sa.UniqueConstraint('slug')
        )

    # 2. Create brand_memberships table
    if not _table_exists('brand_memberships'):
        op.create_table('brand_memberships',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('user_id', sa.Integer(), nullable=False),
            sa.Column('brand_id', sa.Integer(), nullable=False),
            sa.Column('role', sa.String(length=50), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(['brand_id'], ['brands.id']),
            sa.ForeignKeyConstraint(['user_id'], ['users.id']),
            sa.PrimaryKeyConstraint('id'),
            sa.UniqueConstraint('user_id', 'brand_id', name='uq_user_brand')
        )

    # 3. Create usage_records table
    if not _table_exists('usage_records'):
        op.create_table('usage_records',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('brand_id', sa.Integer(), nullable=False),
            sa.Column('user_id', sa.Integer(), nullable=True),
            sa.Column('service', sa.String(length=50), nullable=False),
            sa.Column('operation', sa.String(length=100), nullable=False),
            sa.Column('entity_type', sa.String(length=50), nullable=True),
            sa.Column('entity_id', sa.Integer(), nullable=True),
            sa.Column('units_consumed', sa.Float(), nullable=True),
            sa.Column('estimated_cost_usd', sa.Float(), nullable=True),
            sa.Column('meta_data', sa.JSON(), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(['brand_id'], ['brands.id']),
            sa.ForeignKeyConstraint(['user_id'], ['users.id']),
            sa.PrimaryKeyConstraint('id')
        )
    if not _index_exists('ix_usage_records_brand_id'):
        op.create_index('ix_usage_records_brand_id', 'usage_records', ['brand_id'])
    if not _index_exists('ix_usage_records_created_at'):
        op.create_index('ix_usage_records_created_at', 'usage_records', ['created_at'])

    # 4. Add active_brand_id to users
    if not _column_exists('users', 'active_brand_id'):
        op.add_column('users', sa.Column('active_brand_id', sa.Integer(), nullable=True))
    if not _fk_exists('fk_users_active_brand'):
        op.create_foreign_key('fk_users_active_brand', 'users', 'brands', ['active_brand_id'], ['id'])

    # 5. Add brand_id to existing tables (nullable for backward compatibility)
    if not _column_exists('products', 'brand_id'):
        op.add_column('products', sa.Column('brand_id', sa.Integer(), nullable=True))
    if not _fk_exists('fk_products_brand'):
        op.create_foreign_key('fk_products_brand', 'products', 'brands', ['brand_id'], ['id'])
    if not _index_exists('ix_products_brand_id'):
        op.create_index('ix_products_brand_id', 'products', ['brand_id'])

    # Drop the unique constraint on products.url so same URL can exist across brands
    try:
        op.drop_constraint('products_url_key', 'products', type_='unique')
    except Exception:
        # Constraint may not exist or have a different name
        pass

    if not _column_exists('use_cases', 'brand_id'):
        op.add_column('use_cases', sa.Column('brand_id', sa.Integer(), nullable=True))
    if not _fk_exists('fk_use_cases_brand'):
        op.create_foreign_key('fk_use_cases_brand', 'use_cases', 'brands', ['brand_id'], ['id'])
    if not _index_exists('ix_use_cases_brand_id'):
        op.create_index('ix_use_cases_brand_id', 'use_cases', ['brand_id'])

    if not _column_exists('video_clips', 'brand_id'):
        op.add_column('video_clips', sa.Column('brand_id', sa.Integer(), nullable=True))
    if not _fk_exists('fk_video_clips_brand'):
        op.create_foreign_key('fk_video_clips_brand', 'video_clips', 'brands', ['brand_id'], ['id'])
    if not _index_exists('ix_video_clips_brand_id'):
        op.create_index('ix_video_clips_brand_id', 'video_clips', ['brand_id'])

    if not _column_exists('final_videos', 'brand_id'):
        op.add_column('final_videos', sa.Column('brand_id', sa.Integer(), nullable=True))
    if not _fk_exists('fk_final_videos_brand'):
        op.create_foreign_key('fk_final_videos_brand', 'final_videos', 'brands', ['brand_id'], ['id'])

    if not _column_exists('clip_library', 'brand_id'):
        op.add_column('clip_library', sa.Column('brand_id', sa.Integer(), nullable=True))
    if not _fk_exists('fk_clip_library_brand'):
        op.create_foreign_key('fk_clip_library_brand', 'clip_library', 'brands', ['brand_id'], ['id'])

    if not _column_exists('activity_logs', 'brand_id'):
        op.add_column('activity_logs', sa.Column('brand_id', sa.Integer(), nullable=True))
    if not _fk_exists('fk_activity_logs_brand'):
        op.create_foreign_key('fk_activity_logs_brand', 'activity_logs', 'brands', ['brand_id'], ['id'])
    if not _column_exists('activity_logs', 'user_id'):
        op.add_column('activity_logs', sa.Column('user_id', sa.Integer(), nullable=True))
    if not _fk_exists('fk_activity_logs_user'):
        op.create_foreign_key('fk_activity_logs_user', 'activity_logs', 'users', ['user_id'], ['id'])

    # 6. Backfill: create a "Default" brand, assign all existing data and users
    #    This is done via raw SQL so it works regardless of ORM state.
    #    Use ON CONFLICT / WHERE NOT EXISTS to be idempotent.
    conn = op.get_bind()
    result = conn.execute(sa.text("SELECT 1 FROM brands WHERE slug = 'default'"))
    if not result.scalar():
        op.execute(
            "INSERT INTO brands (id, name, slug, created_at, updated_at) "
            "VALUES (1, 'Default', 'default', NOW(), NOW())"
        )
        # Advance the sequence past the manually-inserted id so the next
        # normal INSERT doesn't collide with id=1.
        op.execute(
            "SELECT setval(pg_get_serial_sequence('brands', 'id'), "
            "COALESCE((SELECT MAX(id) FROM brands), 1))"
        )

    # Assign all existing users as owners of the Default brand (skip if already assigned)
    op.execute(
        "INSERT INTO brand_memberships (user_id, brand_id, role, created_at) "
        "SELECT id, 1, 'owner', NOW() FROM users "
        "WHERE id NOT IN (SELECT user_id FROM brand_memberships WHERE brand_id = 1)"
    )

    # Set active_brand_id for all existing users that don't have one
    op.execute("UPDATE users SET active_brand_id = 1 WHERE active_brand_id IS NULL")

    # Assign all existing data to the Default brand where not already set
    op.execute("UPDATE products SET brand_id = 1 WHERE brand_id IS NULL")
    op.execute("UPDATE use_cases SET brand_id = 1 WHERE brand_id IS NULL")
    op.execute("UPDATE video_clips SET brand_id = 1 WHERE brand_id IS NULL")
    op.execute("UPDATE final_videos SET brand_id = 1 WHERE brand_id IS NULL")
    op.execute("UPDATE clip_library SET brand_id = 1 WHERE brand_id IS NULL")
    op.execute("UPDATE activity_logs SET brand_id = 1 WHERE brand_id IS NULL")


def downgrade():
    # Remove brand_id columns
    op.drop_column('activity_logs', 'user_id')
    op.drop_column('activity_logs', 'brand_id')
    op.drop_column('clip_library', 'brand_id')
    op.drop_column('final_videos', 'brand_id')
    op.drop_column('video_clips', 'brand_id')
    op.drop_column('use_cases', 'brand_id')
    op.drop_column('products', 'brand_id')
    op.drop_column('users', 'active_brand_id')

    # Re-add unique constraint on products.url
    op.create_unique_constraint('products_url_key', 'products', ['url'])

    # Drop new tables
    op.drop_table('usage_records')
    op.drop_table('brand_memberships')
    op.drop_table('brands')
