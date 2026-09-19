"""Regressions for Flutter's Buy now -> saved address -> Continue flow."""
from decimal import Decimal

import pytest
from sqlalchemy import event, select

from app.models.lead import BuyerLead, Order
from app.models.notification import Notification
from tests.test_order_acceptance_coupons import _seed_shop


def checkout_payload(product):
    return {
        "merchant_product_id": str(product.id),
        "delivery_city": "New Delhi",
        "delivery_phone": "+919876543210",
        "delivery_address_line1": "House 12, Floor 2, Block B, Anup Vihar",
        "delivery_state": "Delhi",
        "delivery_pincode": "110093",
        "delivery_latitude": 28.7041,
        "delivery_longitude": 77.1025,
        "items": [{
            "product_id": str(product.id), "qty": 2,
            "price_at_capture": 1000, "title": product.title, "sku": product.sku,
        }],
    }


@pytest.mark.asyncio
async def test_checkout_with_saved_address_returns_the_created_order(auth_client, test_user, db_session):
    merchant, _, product = await _seed_shop(db_session, test_user, slug="address-checkout")
    payload = checkout_payload(product)
    response = await auth_client.post("/api/v1/buyer/leads/", json=payload)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["merchant"]["id"] == str(merchant.id)
    assert Decimal(body["order"]["total_estimated"]) == Decimal("2000")
    assert body["order"]["payment_status"] == "pending"
    assert body["order"]["reward_tokens"] == 0
    for field in ("city", "phone", "address_line1", "state", "pincode", "latitude", "longitude"):
        assert body["order"]["delivery_address"][field] == payload[f"delivery_{field}"]
    history = await auth_client.get("/api/v1/buyer/leads/me")
    assert history.status_code == 200, history.text
    assert history.json()["items"][0]["order"]["id"] == body["order"]["id"]


@pytest.mark.asyncio
async def test_notification_write_failure_does_not_turn_a_saved_order_into_500(auth_client, test_user, db_session):
    _, _, product = await _seed_shop(db_session, test_user, slug="notification-checkout")
    payload = checkout_payload(product)

    def fail_notification(*args):
        raise RuntimeError("Simulated notification storage failure")

    event.listen(Notification, "before_insert", fail_notification)
    try:
        response = await auth_client.post("/api/v1/buyer/leads/", json=payload)
    finally:
        event.remove(Notification, "before_insert", fail_notification)

    assert response.status_code == 201, response.text
    body = response.json()
    # The independent notification transaction is rolled back; the order and
    # selected address stay saved and the DB session remains usable.
    orders = (await db_session.execute(select(Order))).scalars().all()
    leads = (await db_session.execute(select(BuyerLead))).scalars().all()
    assert len(orders) == len(leads) == 1
    assert str(orders[0].id) == body["order"]["id"]
    assert orders[0].delivery_address["address_line1"] == payload["delivery_address_line1"]
    assert (await db_session.execute(select(Notification))).scalars().all() == []
