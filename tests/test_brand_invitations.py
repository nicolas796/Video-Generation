import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app
from app.brand_routes import _build_accept_invitation_url


def test_invite_url_uses_request_host_when_app_base_url_is_default_localhost():
    app = create_app('testing')
    app.config.update(TESTING=True, APP_BASE_URL='http://localhost:5000')

    with app.test_request_context('/', base_url='https://video-generation-zb02.onrender.com'):
        url = _build_accept_invitation_url('abc123')

    assert url == 'https://video-generation-zb02.onrender.com/invite/abc123'


def test_invite_url_uses_configured_external_origin_when_valid():
    app = create_app('testing')
    app.config.update(
        TESTING=True,
        APP_BASE_URL='https://custom.example.com',
        INVITATION_LINK_USE_APP_BASE_URL='true',
    )

    with app.test_request_context('/', base_url='https://video-generation-zb02.onrender.com'):
        url = _build_accept_invitation_url('abc123')

    assert url == 'https://custom.example.com/invite/abc123'


def test_invite_url_ignores_configured_base_unless_opted_in():
    app = create_app('testing')
    app.config.update(TESTING=True, APP_BASE_URL='https://wrong-service.onrender.com')

    with app.test_request_context('/', base_url='https://video-generation-zb02.onrender.com'):
        url = _build_accept_invitation_url('abc123')

    assert url == 'https://video-generation-zb02.onrender.com/invite/abc123'
