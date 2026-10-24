import base64
import io
import uuid

import pytest
import pytest_asyncio
from PIL import Image
from sqlalchemy import select

from app.core.admin_security import create_admin_access_token
from app.models.admin import AdminAccount, AuditLog, Permission, Role
from app.models.home_content import HomeCarousel, HomeCarouselImage
from app.schemas.home_content import default_carousel

PUBLIC = '/api/v1/home/carousel'
ADMIN = '/api/v1/admin/home/carousel'


@pytest_asyncio.fixture
async def admin_headers(db_session):
    async def create(*keys):
        permissions = []
        for key in keys:
            permission = (await db_session.execute(select(Permission).where(Permission.key == key))).scalar_one_or_none()
            if permission is None:
                permission = Permission(key=key)
                db_session.add(permission)
            permissions.append(permission)
        account = AdminAccount(email=f'{uuid.uuid4()}@example.com', full_name='Content Editor')
        role = Role(name=f'Content-{uuid.uuid4()}', is_predefined=False, permissions=permissions)
        account.roles = [role]
        db_session.add(account)
        await db_session.commit()
        return {'Authorization': f'Bearer {create_admin_access_token(str(account.id), roles=[role.name])}'}
    return create


def payload(**overrides):
    return {**default_carousel().model_dump(mode='json'), **overrides}


def slide(id, **overrides):
    return {**payload()['slides'][0], 'id': id, 'title': f'Banner {id}', **overrides}


@pytest.mark.asyncio
async def test_public_default_does_not_write_database(client, db_session):
    response = await client.get(PUBLIC)
    assert response.status_code == 200
    assert response.json()['version'] == 0
    assert response.json()['slides'][0]['id'] == 'welcome'
    assert response.headers['cache-control'] == 'no-cache'
    assert await db_session.get(HomeCarousel, 1) is None


@pytest.mark.asyncio
async def test_publish_filters_disabled_slides_preserves_order_and_audits(client, db_session, admin_headers):
    headers = await admin_headers('content.read', 'content.manage')
    response = await client.put(ADMIN, headers=headers, json=payload(
        slides=[slide('third'), slide('hidden', enabled=False), slide('first')], autoplay=False,
    ))
    assert response.status_code == 200, response.text
    assert response.json()['version'] == 1
    public = (await client.get(PUBLIC)).json()
    assert [s['id'] for s in public['slides']] == ['third', 'first']
    assert public['autoplay'] is False
    private = (await client.get(ADMIN, headers=headers)).json()
    assert [s['id'] for s in private['slides']] == ['third', 'hidden', 'first']
    logs = list((await db_session.execute(select(AuditLog))).scalars())
    assert len(logs) == 1
    assert logs[0].action == 'home.carousel.update'
    assert logs[0].audit_metadata['version'] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('change', [{'enabled': False}, {'slides': []}, {'slides': [slide('hidden', enabled=False)]}])
async def test_intentionally_hidden_carousel_never_restores_default(client, admin_headers, change):
    headers = await admin_headers('content.read', 'content.manage')
    saved = await client.put(ADMIN, headers=headers, json=payload(**change))
    assert saved.status_code == 200
    assert (await client.get(PUBLIC)).json()['slides'] == []
    admin = (await client.get(ADMIN, headers=headers)).json()
    assert admin['version'] == 1
    assert admin['slides'] == saved.json()['slides']


@pytest.mark.asyncio
async def test_stale_saves_return_conflict_without_overwriting(client, admin_headers, db_session):
    headers = await admin_headers('content.read', 'content.manage')
    first = await client.put(ADMIN, headers=headers, json=payload())
    assert first.status_code == 200
    # Two editors both initially loaded version zero.
    stale = await client.put(ADMIN, headers=headers, json=payload(enabled=False))
    assert stale.status_code == 409
    second = await client.put(ADMIN, headers=headers, json=payload(version=1, slides=[slide('new')]))
    assert second.status_code == 200
    stale = await client.put(ADMIN, headers=headers, json=payload(version=1, enabled=False))
    assert stale.status_code == 409
    public = (await client.get(PUBLIC)).json()
    assert public['version'] == 2
    assert public['slides'][0]['id'] == 'new'
    assert len(list((await db_session.execute(select(AuditLog))).scalars())) == 2


@pytest.mark.asyncio
async def test_read_only_admin_cannot_publish_or_upload(client, admin_headers):
    headers = await admin_headers('content.read')
    assert (await client.get(ADMIN, headers=headers)).status_code == 200
    assert (await client.put(ADMIN, headers=headers, json=payload())).status_code == 403
    assert (await client.post(ADMIN + '/images', headers=headers, json={'image_base64': 'eA=='})).status_code == 403


@pytest.mark.asyncio
async def test_unprivileged_admin_cannot_read(client, admin_headers):
    headers = await admin_headers('merchants.read')
    assert (await client.get(ADMIN, headers=headers)).status_code == 403


@pytest.mark.asyncio
async def test_consumer_credentials_cannot_manage_carousel(auth_client):
    assert (await auth_client.get(ADMIN)).status_code == 401
    assert (await auth_client.put(ADMIN, json=payload())).status_code == 401
    assert (await auth_client.get(PUBLIC)).status_code == 200


@pytest.mark.asyncio
async def test_unauthenticated_admin_requests_are_rejected(client):
    assert (await client.get(ADMIN)).status_code in (401, 403)
    assert (await client.put(ADMIN, json=payload())).status_code in (401, 403)


@pytest.mark.asyncio
@pytest.mark.parametrize('changes', [
    {'interval_seconds': 2}, {'interval_seconds': 31},
    {'slides': [slide('same'), slide('same')]},
    {'slides': [slide(str(i)) for i in range(11)]},
    {'slides': [slide('a', title=' ')]},
    {'slides': [slide('a', action='url')]},
    {'slides': [slide('a', action='product', merchant_lookup='shop')]},
    {'slides': [slide('a', action='merchant', merchant_lookup='../settings')]},
    {'slides': [slide('a', button_label='')]},
    {'slides': [slide('a', image_url='javascript:alert(1)')]},
    {'slides': [slide('a', image_url='http://example.com/image.jpg')]},
    {'slides': [slide('a', image_url='https://secret:pass@example.com/image.jpg')]},
    {'slides': [slide('a', image_url='/api/v1/uploads/private-image')]},
    {'unknown': 'not allowed'},
])
async def test_invalid_publication_is_rejected(client, admin_headers, changes):
    headers = await admin_headers('content.manage')
    response = await client.put(ADMIN, headers=headers, json=payload(**changes))
    assert response.status_code == 422, response.text
    assert (await client.get(PUBLIC)).json()['version'] == 0


@pytest.mark.asyncio
async def test_upload_reencodes_and_exposes_only_carousel_images(client, admin_headers, db_session):
    headers = await admin_headers('content.manage')
    source = io.BytesIO()
    Image.new('RGB', (2400, 800), 'green').save(source, format='PNG')
    uploaded = await client.post(ADMIN + '/images', headers=headers, json={
        'image_base64': base64.b64encode(source.getvalue()).decode(),
    })
    assert uploaded.status_code == 201, uploaded.text
    image = await client.get(uploaded.json()['url'])
    assert image.status_code == 200
    assert image.headers['content-type'] == 'image/jpeg'
    assert image.headers['x-content-type-options'] == 'nosniff'
    assert 'immutable' in image.headers['cache-control']
    decoded = Image.open(io.BytesIO(image.content))
    assert decoded.size == (1920, 640)
    assert decoded.format == 'JPEG'
    assert not decoded.getexif()
    assert uploaded.json()['byte_size'] == len(image.content)
    assert (await client.get(PUBLIC + '/images/' + str(uuid.uuid4()))).status_code == 404
    assert len(list((await db_session.execute(select(HomeCarouselImage))).scalars())) == 1
    logs = list((await db_session.execute(select(AuditLog))).scalars())
    assert logs[0].action == 'home.carousel.image.upload'


@pytest.mark.asyncio
@pytest.mark.parametrize('encoded', ['bad base64!', base64.b64encode(b'<svg>not an image</svg>').decode()])
async def test_upload_rejects_non_images(client, admin_headers, encoded):
    headers = await admin_headers('content.manage')
    response = await client.post(ADMIN + '/images', headers=headers, json={'image_base64': encoded})
    assert response.status_code == 422


def test_image_processor_rejects_oversize_payload():
    from fastapi import HTTPException
    from app.services.home_content import prepare_carousel_image
    with pytest.raises(HTTPException) as error:
        prepare_carousel_image(base64.b64encode(b'x' * (5 * 1024 * 1024 + 1)).decode())
    assert error.value.status_code == 413
