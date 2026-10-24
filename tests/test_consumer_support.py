import uuid

import pytest
import pytest_asyncio
from sqlalchemy import func, select

from app.core.admin_security import create_admin_access_token
from app.core.security import create_access_token
from app.models.admin import AdminAccount, AuditLog, Permission, Role
from app.models.notification import Notification
from app.models.session import DesignSession
from app.models.support import SupportTicket, SupportMessage

ROOT = '/api/v1/support'
ADMIN = '/api/v1/admin/support'


@pytest_asyncio.fixture
async def support_admin(db_session):
    async def headers(*keys):
        grants = []
        for key in keys:
            grant = await db_session.scalar(select(Permission).where(Permission.key == key))
            if grant is None:
                grant = Permission(key=key)
                db_session.add(grant)
            grants.append(grant)
        role = Role(name=str(uuid.uuid4()), permissions=grants)
        admin = AdminAccount(email=f'{uuid.uuid4()}@example.com', roles=[role])
        db_session.add(admin)
        await db_session.commit()
        return {'Authorization': f'Bearer {create_admin_access_token(str(admin.id), roles=[role.name])}'}
    return headers


def request(**changes):
    return {'subject': 'Help with my design', 'description': 'The product looks different in the generated image.',
            'category': 'ai_design', 'client_request_id': uuid.uuid4().hex, **changes}


@pytest.mark.asyncio
async def test_customer_create_list_read_and_idempotent_retry(auth_client, db_session):
    body = request()
    created = await auth_client.post(ROOT + '/tickets', json=body)
    assert created.status_code == 201, created.text
    data = created.json()
    assert len(data['reference']) == 6
    assert data['messages'][0]['body'] == body['description']
    assert 'author_id' not in data['messages'][0]
    assert (await auth_client.post(ROOT + '/tickets', json=body)).json()['id'] == data['id']
    assert (await db_session.scalar(select(func.count()).select_from(SupportTicket))) == 1
    assert (await auth_client.get(ROOT + '/tickets')).json()['total'] == 1
    assert (await auth_client.get(ROOT + '/tickets/' + data['id'])).status_code == 200
    conflict = await auth_client.post(ROOT + '/tickets', json={**body, 'subject': 'Different request'})
    assert conflict.status_code == 409


@pytest.mark.asyncio
async def test_customer_cannot_read_or_reply_to_other_customer_ticket(auth_client, test_user, db_session):
    ticket = SupportTicket(subject='Private', requester_type='consumer', requester_id=uuid.uuid4())
    db_session.add(ticket)
    await db_session.commit()
    assert (await auth_client.get(ROOT + '/tickets')).json()['total'] == 0
    assert (await auth_client.get(f'{ROOT}/tickets/{ticket.id}')).status_code == 404
    assert (await auth_client.post(f'{ROOT}/tickets/{ticket.id}/messages', json={
        'body': 'Try to read', 'client_message_id': uuid.uuid4().hex,
    })).status_code == 404


@pytest.mark.asyncio
async def test_customer_cannot_attach_another_users_design(auth_client, db_session):
    session = DesignSession(user_id=uuid.uuid4(), title='Private design')
    db_session.add(session)
    await db_session.commit()
    response = await auth_client.post(ROOT + '/tickets', json=request(session_id=str(session.id)))
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_customer_can_attach_owned_design(auth_client, db_session, test_user):
    session = DesignSession(user_id=test_user.id, title='My design')
    db_session.add(session)
    await db_session.commit()
    response = await auth_client.post(ROOT + '/tickets', json=request(session_id=str(session.id)))
    assert response.status_code == 201
    assert response.json()['session_id'] == str(session.id)


@pytest.mark.asyncio
async def test_admin_reply_status_and_consumer_followup_with_notifications(auth_client, client, support_admin, db_session, test_user):
    created = (await auth_client.post(ROOT + '/tickets', json=request())).json()
    ticket_id = created['id']
    admin = await support_admin('support.read', 'support.respond')
    listed = await client.get(ADMIN + '/consumer', headers=admin)
    assert listed.status_code == 200, listed.text
    assert listed.json()['items'][0]['reference'] == created['reference']
    detail = await client.get(f'{ADMIN}/tickets/{ticket_id}', headers=admin)
    assert detail.status_code == 200, detail.text
    assert len(detail.json()['messages']) == 1
    message = {'body': 'We can help you with this.', 'client_message_id': uuid.uuid4().hex}
    response = await client.post(f'{ADMIN}/tickets/{ticket_id}/messages', headers=admin, json=message)
    assert response.status_code == 200, response.text
    assert len(response.json()['messages']) == 2
    assert response.json()['status'] == 'pending'
    assert (await client.post(f'{ADMIN}/tickets/{ticket_id}/messages', headers=admin, json=message)).status_code == 200
    assert (await db_session.scalar(select(func.count()).select_from(SupportMessage))) == 2
    notifications = list((await db_session.scalars(select(Notification).where(Notification.user_id == test_user.id))).all())
    assert len(notifications) == 1
    assert notifications[0].payload['ticket_id'] == ticket_id
    assert notifications[0].payload['requester_type'] == 'consumer'
    closed = await client.patch(f'{ADMIN}/tickets/{ticket_id}/status', headers=admin, json={'status': 'resolved'})
    assert closed.status_code == 200
    await client.patch(f'{ADMIN}/tickets/{ticket_id}/status', headers=admin, json={'status': 'resolved'})
    assert (await db_session.scalar(select(func.count()).select_from(Notification))) == 2
    followup = {'body': 'I still need help please', 'client_message_id': uuid.uuid4().hex}
    consumer = {'Authorization': f'Bearer {create_access_token(str(test_user.id))}'}
    reopened = await client.post(f'{ROOT}/tickets/{ticket_id}/messages', headers=consumer, json=followup)
    assert reopened.status_code == 200, reopened.text
    assert reopened.json()['status'] == 'open'
    assert len(reopened.json()['messages']) == 3
    retried = await client.post(f'{ROOT}/tickets/{ticket_id}/messages', headers=consumer, json=followup)
    assert len(retried.json()['messages']) == 3
    audit = list((await db_session.scalars(select(AuditLog))).all())
    assert {'support.respond', 'support.status'} <= {row.action for row in audit}


@pytest.mark.asyncio
async def test_settings_can_pause_new_requests_without_blocking_existing_replies(auth_client, client, support_admin):
    created = (await auth_client.post(ROOT + '/tickets', json=request())).json()
    admin = await support_admin('content.read', 'content.manage')
    config = (await client.get(ADMIN + '/settings', headers=admin)).json()
    response = await client.put(ADMIN + '/settings', headers=admin, json={**config,
        'accepting_tickets': False, 'title': 'Contact our team',
        'faqs': [{'question': 'Where are my orders?', 'answer': 'Open Settings, then Your orders.'}],
    })
    assert response.status_code == 200, response.text
    assert response.json()['version'] == 1
    assert (await client.get(ROOT + '/settings')).json()['title'] == 'Contact our team'
    assert (await auth_client.post(ROOT + '/tickets', json=request())).status_code == 503
    followup = await auth_client.post(f"{ROOT}/tickets/{created['id']}/messages", json={
        'body': 'Following up', 'client_message_id': uuid.uuid4().hex,
    })
    assert followup.status_code == 200
    assert (await client.put(ADMIN + '/settings', headers=admin, json=config)).status_code == 409


@pytest.mark.asyncio
async def test_support_permissions_are_enforced(auth_client, client, support_admin):
    assert (await auth_client.get(ADMIN + '/settings')).status_code == 401
    readonly = await support_admin('support.read', 'content.read')
    config = (await client.get(ROOT + '/settings')).json()
    assert (await client.put(ADMIN + '/settings', headers=readonly, json=config)).status_code == 403
    assert (await client.post(f'{ADMIN}/tickets/{uuid.uuid4()}/messages', headers=readonly, json={'body': 'Hello'})).status_code == 403


@pytest.mark.asyncio
@pytest.mark.parametrize('changes', [{'subject': '  '}, {'description': ' '}, {'category': 'admin'}, {'requester_id': str(uuid.uuid4())}])
async def test_invalid_tickets_rejected(auth_client, changes):
    assert (await auth_client.post(ROOT + '/tickets', json=request(**changes))).status_code == 422
