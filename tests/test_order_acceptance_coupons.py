from datetime import datetime, timezone
from decimal import Decimal
import uuid

import pytest
from sqlalchemy import select


async def _seed_shop(db_session, user, *, slug: str):
    from app.models.merchant import MemberRole, Merchant, MerchantMember
    from app.models.merchant_product import MerchantProduct
    from app.models.wallet import PricingRule, Wallet

    merchant = Merchant(
        slug=slug,
        legal_name=f"{slug} Legal",
        display_name=slug.title(),
        referral_code=f"{slug.upper()}-1",
        is_kyc_completed=True,
    )
    db_session.add(merchant)
    await db_session.flush()
    db_session.add(
        MerchantMember(
            merchant_id=merchant.id,
            user_id=user.id,
            role=MemberRole.OWNER.value,
        )
    )
    wallet = Wallet(merchant_id=merchant.id, balance=Decimal("1000"))
    product = MerchantProduct(
        merchant_id=merchant.id,
        sku=f"{slug.upper()}-SKU",
        title="Coupon Sofa",
        in_app_price=Decimal("1000"),
        status="published",
        has_simulafly_listing=True,
    )
    db_session.add_all([wallet, product])
    db_session.add(
        PricingRule(
            event_type="simulafly_purchase",
            merchant_id=merchant.id,
            rate=Decimal("5"),
            rate_type="percentage",
            effective_from=datetime.now(timezone.utc),
        )
    )
    await db_session.commit()
    await db_session.refresh(merchant)
    await db_session.refresh(wallet)
    await db_session.refresh(product)
    return merchant, wallet, product


@pytest.mark.asyncio
async def test_coupon_is_server_calculated_and_fee_is_charged_on_acceptance(
    auth_client,
    test_user,
    db_session,
):
    from app.models.event import LedgerEntry
    from app.models.lead import Order

    merchant, wallet, product = await _seed_shop(
        db_session, test_user, slug="acceptance-shop"
    )
    headers = {"X-Merchant-Id": str(merchant.id)}

    coupon_response = await auth_client.post(
        "/api/v1/coupons/merchant/create",
        headers=headers,
        json={
            "code": "save10",
            "title": "Ten percent off",
            "discount_type": "percentage",
            "discount_value": 10,
            "min_order_amount": 100,
            "usage_limit": 2,
        },
    )
    assert coupon_response.status_code == 201, coupon_response.text
    assert coupon_response.json()["merchant_id"] == str(merchant.id)

    order_response = await auth_client.post(
        "/api/v1/buyer/leads/",
        json={
            "merchant_product_id": str(product.id),
            "coupon_code": "save10",
            # A manipulated client value must never control the actual discount.
            "discount_amount": 9999,
            "items": [
                {
                    "product_id": str(product.id),
                    "qty": 1,
                    "price_at_capture": 1000,
                    "title": product.title,
                    "sku": product.sku,
                }
            ],
        },
    )
    assert order_response.status_code == 201, order_response.text
    body = order_response.json()
    assert float(body["order"]["subtotal_estimated"]) == 1000
    assert float(body["order"]["discount_amount"]) == 100
    assert float(body["order"]["total_estimated"]) == 900
    assert body["order"]["coupon_code"] == "SAVE10"

    lead_id = body["id"]
    accepted = await auth_client.patch(
        f"/api/v1/merchant/leads/{lead_id}",
        headers=headers,
        json={"status": "synced"},
    )
    assert accepted.status_code == 200, accepted.text
    accepted_order = accepted.json()["order"]
    assert accepted_order["accepted_at"] is not None
    assert accepted_order["fee_charged_at"] is not None
    assert float(accepted_order["platform_fee_amount"]) == 45

    await db_session.refresh(wallet)
    assert wallet.balance == Decimal("955")
    ledger_rows = list(
        (
            await db_session.execute(
                select(LedgerEntry).where(
                    LedgerEntry.merchant_id == merchant.id,
                    LedgerEntry.reason == "order_confirmation",
                )
            )
        ).scalars()
    )
    assert len(ledger_rows) == 1
    assert ledger_rows[0].amount == Decimal("-45")
    assert ledger_rows[0].related_order_id == uuid.UUID(accepted_order["id"])

    history = await auth_client.get(
        "/api/v1/merchant/wallet/balance-history",
        headers=headers,
    )
    assert history.status_code == 200, history.text
    service_fee = next(
        item for item in history.json()["items"]
        if item["reason"] == "order_confirmation"
    )
    assert service_fee["reference_id"] is None
    assert service_fee["order"]["id"] == accepted_order["id"]
    assert service_fee["order"]["buyer_name"] == test_user.full_name
    assert service_fee["order"]["items"][0] == {
        "product_id": str(product.id),
        "title": "Coupon Sofa",
        "sku": product.sku,
        "quantity": 1,
    }
    assert service_fee["order"]["total_value"] == 900.0

    # Re-accepting and later completing the same order must not charge again.
    repeated = await auth_client.patch(
        f"/api/v1/merchant/leads/{lead_id}",
        headers=headers,
        json={"status": "synced"},
    )
    assert repeated.status_code == 200
    completed = await auth_client.patch(
        f"/api/v1/merchant/leads/{lead_id}",
        headers=headers,
        json={"status": "converted"},
    )
    assert completed.status_code == 200

    await db_session.refresh(wallet)
    assert wallet.balance == Decimal("955")
    order = (
        await db_session.execute(
            select(Order).where(Order.lead_id == uuid.UUID(body["id"]))
        )
    ).scalar_one()
    assert order.coupon_code == "SAVE10"
    assert order.discount_amount == Decimal("100")


@pytest.mark.asyncio
async def test_only_authenticated_merchant_can_create_owned_coupon(
    client,
    auth_client,
    test_user,
    db_session,
):
    merchant, _, _ = await _seed_shop(db_session, test_user, slug="coupon-owner")
    headers = {"X-Merchant-Id": str(merchant.id)}
    payload = {
        "code": "OWNER20",
        "title": "Owner coupon",
        "discount_type": "flat",
        "discount_value": 20,
    }

    unauthenticated = await client.post(
        "/api/v1/coupons/merchant/create",
        headers={**headers, "Authorization": ""},
        json=payload,
    )
    assert unauthenticated.status_code in {401, 403}

    caller_selected_owner = await auth_client.post(
        "/api/v1/coupons/merchant/create",
        headers=headers,
        json={**payload, "merchant_id": "00000000-0000-0000-0000-000000000001"},
    )
    assert caller_selected_owner.status_code == 422


@pytest.mark.asyncio
async def test_coupon_cannot_be_used_for_another_merchant(
    auth_client,
    test_user,
    db_session,
):
    first, _, _ = await _seed_shop(db_session, test_user, slug="coupon-first")
    second, _, _ = await _seed_shop(db_session, test_user, slug="coupon-second")

    created = await auth_client.post(
        "/api/v1/coupons/merchant/create",
        headers={"X-Merchant-Id": str(first.id)},
        json={
            "code": "PRIVATE10",
            "title": "First shop only",
            "discount_type": "percentage",
            "discount_value": 10,
        },
    )
    assert created.status_code == 201, created.text

    validation = await auth_client.post(
        "/api/v1/coupons/validate",
        json={
            "code": "PRIVATE10",
            "merchant_id": str(second.id),
            "order_amount": 1000,
        },
    )
    assert validation.status_code == 200
    assert validation.json()["valid"] is False
