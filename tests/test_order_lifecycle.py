from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from app.models.lead import Order
from app.models.notification import Notification
from tests.test_order_acceptance_coupons import _seed_shop


async def place(auth_client, db_session, user, slug="progress-shop"):
    merchant, wallet, product = await _seed_shop(db_session, user, slug=slug)
    response = await auth_client.post("/api/v1/buyer/leads/", json={"merchant_product_id": str(product.id), "delivery_address_line1": "Test delivery address"})
    assert response.status_code == 201, response.text
    return response.json(), {"X-Merchant-Id": str(merchant.id)}, merchant, wallet


@pytest.mark.asyncio
@pytest.mark.parametrize("payment_first", [True, False])
async def test_complete_only_after_delivery_and_payment_and_reward_once(auth_client, test_user, db_session, payment_first):
    body, headers, _, wallet = await place(auth_client, db_session, test_user)
    path = f"/api/v1/merchant/leads/{body['id']}"
    initial = test_user.credit_balance
    events = [{"payment_status": "paid"}, {"fulfillment_status": "fulfilled"}]
    if not payment_first:
        events.reverse()
    first = await auth_client.patch(path, headers=headers, json=events[0])
    assert first.status_code == 200, first.text
    assert first.json()["order"]["status"] != "completed"
    await db_session.refresh(test_user)
    assert test_user.credit_balance == initial
    second = await auth_client.patch(path, headers=headers, json=events[1])
    assert second.status_code == 200, second.text
    order = second.json()["order"]
    assert order["status"] == "completed"
    assert order["payment_completed_at"] and order["delivered_at"] and order["completed_at"]
    assert order["reward_tokens"] == 10 and order["reward_granted_at"]
    for _ in range(3):
        retry = await auth_client.patch(path, headers=headers, json={"status": "converted", "payment_status": "paid", "fulfillment_status": "fulfilled"})
        assert retry.status_code == 200, retry.text
    await db_session.refresh(test_user)
    await db_session.refresh(wallet)
    assert test_user.credit_balance == initial + 10
    assert float(wallet.balance) == 950
    notices = (await db_session.execute(select(Notification).where(Notification.title == "Order complete · +10 tokens"))).scalars().all()
    assert len(notices) == 1


@pytest.mark.asyncio
async def test_legacy_converted_means_payment_not_delivery(auth_client, test_user, db_session):
    body, headers, _, _ = await place(auth_client, db_session, test_user)
    response = await auth_client.patch(f"/api/v1/merchant/leads/{body['id']}", headers=headers, json={"status": "converted"})
    assert response.status_code == 200, response.text
    assert response.json()["order"]["payment_status"] == "paid"
    assert response.json()["order"]["status"] == "contacted"
    assert response.json()["order"]["reward_tokens"] == 0


@pytest.mark.asyncio
async def test_progress_cannot_regress_or_revive_cancelled_order(auth_client, test_user, db_session):
    body, headers, _, _ = await place(auth_client, db_session, test_user)
    path = f"/api/v1/merchant/leads/{body['id']}"
    for stage in ("in_progress", "shipped", "out_for_delivery"):
        response = await auth_client.patch(path, headers=headers, json={"fulfillment_status": stage})
        assert response.status_code == 200, response.text
    backwards = await auth_client.patch(path, headers=headers, json={"fulfillment_status": "pending"})
    assert backwards.status_code == 409
    cancel = await auth_client.patch(path, headers=headers, json={"status": "lost"})
    assert cancel.status_code == 200
    revive = await auth_client.patch(path, headers=headers, json={"fulfillment_status": "fulfilled", "payment_status": "paid"})
    assert revive.status_code == 409
    await db_session.refresh(test_user)
    assert test_user.credit_balance == 20


@pytest.mark.asyncio
async def test_old_completed_order_gets_no_retroactive_reward(auth_client, test_user, db_session):
    body, headers, _, _ = await place(auth_client, db_session, test_user)
    order = (await db_session.execute(select(Order))).scalar_one()
    order.status = "completed"
    order.completed_at = datetime.now(timezone.utc)
    await db_session.commit()
    response = await auth_client.patch(f"/api/v1/merchant/leads/{body['id']}", headers=headers, json={"fulfillment_status": "fulfilled", "payment_status": "paid"})
    assert response.status_code == 409
    await db_session.refresh(test_user)
    assert test_user.credit_balance == 20


@pytest.mark.asyncio
async def test_seller_snapshot_and_merchant_ownership(auth_client, test_user, db_session):
    body, _, merchant, _ = await place(auth_client, db_session, test_user)
    assert body["merchant"]["display_name"] == merchant.display_name
    original = merchant.display_name
    merchant.display_name = "Renamed shop"
    second, _, _ = await _seed_shop(db_session, test_user, slug="other-shop")
    denied = await auth_client.patch(f"/api/v1/merchant/leads/{body['id']}", headers={"X-Merchant-Id": str(second.id)}, json={"payment_status": "paid"})
    assert denied.status_code == 404
    history = await auth_client.get("/api/v1/buyer/leads/me")
    assert history.json()["items"][0]["merchant"]["display_name"] == original


@pytest.mark.asyncio
async def test_preacceptance_address_is_masked_and_tokens_cannot_be_self_awarded(auth_client, test_user, db_session):
    body, headers, _, _ = await place(auth_client, db_session, test_user)
    response = await auth_client.get(f"/api/v1/merchant/leads/{body['id']}", headers=headers)
    assert response.json()["order"]["delivery_address"] == {}
    forged = await auth_client.patch("/api/v1/users/me", json={"credit_balance": 999})
    assert forged.status_code == 422


@pytest.mark.asyncio
async def test_price_and_seller_are_taken_from_catalog(auth_client, test_user, db_session):
    first, _, product = await _seed_shop(db_session, test_user, slug="price-shop")
    _, _, other_product = await _seed_shop(db_session, test_user, slug="different-shop")
    item = {"product_id": str(product.id), "qty": 2, "price_at_capture": 1, "title": "Forged title", "sku": "FORGED"}
    response = await auth_client.post("/api/v1/buyer/leads/", json={"merchant_product_id": str(product.id), "items": [item]})
    assert response.status_code == 201, response.text
    order = response.json()["order"]
    assert float(order["total_estimated"]) == 2000
    assert order["items"][0]["title"] == product.title
    assert response.json()["merchant"]["id"] == str(first.id)
    mixed = await auth_client.post("/api/v1/buyer/leads/", json={"merchant_product_id": str(product.id), "items": [item, {**item, "product_id": str(other_product.id)}]})
    assert mixed.status_code == 422


@pytest.mark.asyncio
async def test_admin_delivery_uses_same_completion_and_reward_guard(auth_client, test_user, db_session):
    from app.models.lead import FulfillmentStatus
    from app.services.admin.booking_service import BookingService
    body, headers, _, _ = await place(auth_client, db_session, test_user)
    response = await auth_client.patch(f"/api/v1/merchant/leads/{body['id']}", headers=headers, json={"payment_status": "paid"})
    assert response.status_code == 200
    order = (await db_session.execute(select(Order))).scalar_one()
    for _ in range(2):
        await BookingService(db_session).update_fulfillment(order.id, FulfillmentStatus.FULFILLED)
    await db_session.refresh(test_user)
    assert order.status == "completed"
    assert order.reward_tokens == 10
    assert test_user.credit_balance == 30
