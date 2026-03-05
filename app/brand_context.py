"""Brand context middleware and access control for multi-tenant support."""
from functools import wraps

from flask import g, session, abort, redirect, url_for, request
from flask_login import current_user

from app import db
from app.models import Brand, BrandMembership


def load_brand_context():
    """Before-request handler: set g.current_brand from session or user default.

    Called on every authenticated request. If no brand is selected yet, auto-selects
    the user's first brand (or creates a "Default" brand for admin users).
    """
    g.current_brand = None

    if not current_user.is_authenticated:
        return

    # Try session first
    brand_id = session.get('active_brand_id')
    if brand_id:
        brand = db.session.get(Brand, brand_id)
        if brand and current_user.has_brand_access(brand_id):
            g.current_brand = brand
            return

    # Fall back to user's stored active_brand_id
    if current_user.active_brand_id:
        brand = db.session.get(Brand, current_user.active_brand_id)
        if brand and current_user.has_brand_access(current_user.active_brand_id):
            g.current_brand = brand
            session['active_brand_id'] = brand.id
            return

    # Auto-select first available brand
    membership = BrandMembership.query.filter_by(user_id=current_user.id).first()
    if membership:
        g.current_brand = membership.brand
        session['active_brand_id'] = membership.brand_id
        current_user.active_brand_id = membership.brand_id
        db.session.commit()


def require_brand(f):
    """Decorator: ensure g.current_brand is set, redirect to brand selection otherwise."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not g.get('current_brand'):
            return redirect(url_for('brand.select_brand'))
        return f(*args, **kwargs)
    return decorated


def require_brand_role(min_role='member'):
    """Decorator factory: ensure user has at least min_role on the active brand."""
    def decorator(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            brand = g.get('current_brand')
            if not brand:
                return redirect(url_for('brand.select_brand'))
            if not current_user.has_brand_access(brand.id, min_role):
                abort(403, 'Insufficient permissions for this brand.')
            return f(*args, **kwargs)
        return decorated
    return decorator


def get_brand_api_key(service):
    """Get API key for a service, preferring brand-specific key over global config.

    Args:
        service: One of 'pollo', 'elevenlabs', 'openai'

    Returns:
        API key string or empty string if not configured.
    """
    from flask import current_app

    brand = g.get('current_brand')
    if brand:
        key_attr = f'{service}_api_key'
        brand_key = getattr(brand, key_attr, None)
        if brand_key:
            return brand_key

    # Fall back to global config
    config_map = {
        'pollo': 'POLLO_API_KEY',
        'elevenlabs': 'ELEVENLABS_API_KEY',
        'openai': 'OPENAI_API_KEY',
    }
    return current_app.config.get(config_map.get(service, ''), '')


def record_usage(service, operation, entity_type=None, entity_id=None,
                 units_consumed=0, estimated_cost_usd=0, meta_data=None):
    """Record an API usage event for the current brand.

    Call this from services after making external API calls.
    """
    from app.models import UsageRecord

    brand = g.get('current_brand')
    if not brand:
        return None

    record = UsageRecord(
        brand_id=brand.id,
        user_id=current_user.id if current_user.is_authenticated else None,
        service=service,
        operation=operation,
        entity_type=entity_type,
        entity_id=entity_id,
        units_consumed=units_consumed,
        estimated_cost_usd=estimated_cost_usd,
        meta_data=meta_data or {},
    )
    db.session.add(record)
    db.session.commit()
    return record
