import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app, db
from app.models import Product, UseCase, Script


@pytest.fixture
def app():
    app = create_app('testing')
    app.config.update({
        'TESTING': True,
        'SQLALCHEMY_DATABASE_URI': 'sqlite:///:memory:',
        'UPLOAD_FOLDER': '/tmp/test_uploads',
        'WTF_CSRF_ENABLED': False,
    })

    with app.app_context():
        db.create_all()
        yield app
        db.drop_all()


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def use_case_with_script(app):
    with app.app_context():
        product = Product(name='Routing Product', url='https://example.com/p/1', description='desc')
        db.session.add(product)
        db.session.flush()

        use_case = UseCase(product_id=product.id, name='Routing UC', style='realistic', generation_mode='balanced')
        use_case.sync_num_clips()
        db.session.add(use_case)
        db.session.flush()

        script = Script(use_case_id=use_case.id, content='Hook line. Problem line. Solution line. CTA line.', status='approved')
        db.session.add(script)
        db.session.commit()

        return {'use_case_id': use_case.id, 'product_id': product.id}


def test_storyboard_plan_respects_clip_count(client, use_case_with_script):
    use_case_id = use_case_with_script['use_case_id']
    response = client.post(
        f'/api/use-cases/{use_case_id}/storyboard-plan',
        json={'clip_count': 2, 'generation_mode': 'balanced', 'scene_template': 'none'}
    )
    assert response.status_code == 200
    data = json.loads(response.data)
    assert data['success'] is True
    assert data['target_clips'] == 2
    assert len(data['storyboard_plan']) == 2


def test_clip_strategy_overrides_persist(client, use_case_with_script):
    use_case_id = use_case_with_script['use_case_id']
    payload = {
        'clip_strategy_overrides': {
            '0': {
                'generation_strategy': 'world_model_broll',
                'model_choice': 'sora-2',
                'use_image': False,
            }
        }
    }
    response = client.put(f'/api/use-cases/{use_case_id}/clip-strategy-overrides', json=payload)
    assert response.status_code == 200
    data = json.loads(response.data)
    assert data['success'] is True
    assert data['clip_strategy_overrides']['0']['generation_strategy'] == 'world_model_broll'

    response = client.post(
        f'/api/use-cases/{use_case_id}/storyboard-plan',
        json={'clip_count': 1, 'scene_template': 'none'}
    )
    assert response.status_code == 200
    plan = json.loads(response.data)['storyboard_plan']
    assert plan[0]['generation_strategy'] == 'world_model_broll'
    assert plan[0]['use_image'] is False
