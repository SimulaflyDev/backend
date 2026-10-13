import uuid
from decimal import Decimal

import pytest


@pytest.mark.asyncio
async def test_wallet_one_per_merchant_uniqueness(db_session, test_user):
    from app.models.merchant import Merchant, MerchantMember, MemberRole
    from app.models.wallet import Wallet

    m = Merchant(slug="w1", legal_name="Wallet One", display_name="W1", referral_code="W1-1")
    db_session.add(m)
    await db_session.commit()
    await db_session.refresh(m)
    db_session.add(MerchantMember(merchant_id=m.id, user_id=test_user.id, role=MemberRole.OWNER.value))
    await db_session.commit()

    w1 = Wallet(merchant_id=m.id)
    db_session.add(w1)
    await db_session.commit()
    await db_session.refresh(w1)
    assert w1.currency == "INR"
    assert Decimal(str(w1.balance)) == Decimal("0")
    assert w1.status == "active"

    w2 = Wallet(merchant_id=m.id)
    db_session.add(w2)
    with pytest.raises(Exception):
        await db_session.commit()
    await db_session.rollback()


@pytest.mark.asyncio
async def test_transaction_gateway_ref_unique_when_set(db_session, test_user):
    from app.models.merchant import Merchant, MerchantMember, MemberRole
    from app.models.wallet import Transaction

    m = Merchant(slug="t1", legal_name="Txn One", display_name="T1", referral_code="T1-1")
    db_session.add(m)
    await db_session.commit()
    await db_session.refresh(m)
    db_session.add(MerchantMember(merchant_id=m.id, user_id=test_user.id, role=MemberRole.OWNER.value))
    await db_session.commit()

    t1 = Transaction(
        merchant_id=m.id,
        amount=Decimal("1000"),
        status="successful",
        gateway_ref="pay_ABCDEF",
    )
    db_session.add(t1)
    await db_session.commit()

    t2 = Transaction(
        merchant_id=m.id,
        amount=Decimal("2000"),
        status="successful",
        gateway_ref="pay_ABCDEF",
    )
    db_session.add(t2)
    with pytest.raises(Exception):
        await db_session.commit()
    await db_session.rollback()


@pytest.mark.asyncio
async def test_transaction_allows_multiple_pending_without_gateway_ref(db_session, test_user):
    from app.models.merchant import Merchant, MerchantMember, MemberRole
    from app.models.wallet import Transaction

    m = Merchant(slug="t2", legal_name="Txn Two", display_name="T2", referral_code="T2-1")
    db_session.add(m)
    await db_session.commit()
    await db_session.refresh(m)
    db_session.add(MerchantMember(merchant_id=m.id, user_id=test_user.id, role=MemberRole.OWNER.value))
    await db_session.commit()

    for i in range(3):
        db_session.add(
            Transaction(
                merchant_id=m.id,
                amount=Decimal("500"),
                status="pending",
                razorpay_order_id=f"order_{i}",
            )
        )
    await db_session.commit()  # should NOT raise


def test_pricing_rule_enums():
    from app.models.wallet import PricingRule, RateType

    assert RateType.FIXED.value == "fixed"
    assert RateType.PERCENTAGE.value == "percentage"
    pr = PricingRule(event_type="click", rate=Decimal("0.25"), rate_type="fixed")
    assert pr.rate_type == "fixed"


def test_topup_intent_request_validates_amount():
    from app.schemas.wallet import TopupIntentRequest
    from pydantic import ValidationError

    valid = TopupIntentRequest(amount=1000)
    assert valid.currency == "INR"

    with pytest.raises(ValidationError):
        TopupIntentRequest(amount=499)
    with pytest.raises(ValidationError):
        TopupIntentRequest(amount=0.99)
    with pytest.raises(ValidationError):
        TopupIntentRequest(amount=-50)
    with pytest.raises(ValidationError):
        TopupIntentRequest(amount=10_000_000)
    with pytest.raises(ValidationError):
        TopupIntentRequest(amount=100, currency="USD")


def test_topup_confirm_request_requires_all_fields():
    from app.schemas.wallet import TopupConfirmRequest
    from pydantic import ValidationError

    valid = TopupConfirmRequest(
        order_id="order_AB",
        payment_id="pay_XY",
        signature="abc123",
    )
    assert valid.order_id == "order_AB"

    with pytest.raises(ValidationError):
        TopupConfirmRequest(order_id="x", payment_id="y")


def test_wallet_settings_update_threshold_only():
    from app.schemas.wallet import WalletSettingsUpdate
    from pydantic import ValidationError

    valid = WalletSettingsUpdate(low_balance_threshold=2500)
    assert valid.low_balance_threshold == 2500

    empty = WalletSettingsUpdate()
    assert empty.low_balance_threshold is None

    with pytest.raises(ValidationError):
        WalletSettingsUpdate(low_balance_threshold=-100)


@pytest.mark.asyncio
async def test_create_merchant_also_creates_wallet(auth_client, db_session):
    import uuid as _uuid
    from sqlalchemy import select
    from app.models.wallet import Wallet

    r = await auth_client.post(
        "/api/v1/merchants/",
        json={"legal_name": "Auto Wallet Co", "display_name": "Auto Wallet"},
    )
    assert r.status_code == 201
    mid = _uuid.UUID(r.json()["id"])

    res = await db_session.execute(
        select(Wallet).where(Wallet.merchant_id == mid)
    )
    wallet = res.scalar_one_or_none()
    assert wallet is not None
    assert wallet.currency == "INR"
    assert float(wallet.balance) == 0.0
    assert wallet.status == "active"


@pytest.mark.asyncio
async def test_get_wallet_returns_zero_balance_for_new_merchant(auth_client, verify_merchant):
    r = await auth_client.post(
        "/api/v1/merchants/", json={"legal_name": "Wallet Read", "display_name": "WR"}
    )
    mid = r.json()["id"]
    await verify_merchant(mid)

    r = await auth_client.get(
        "/api/v1/merchant/wallet/", headers={"X-Merchant-Id": mid}
    )
    assert r.status_code == 200
    body = r.json()
    assert body["currency"] == "INR"
    assert body["balance"] == 0.0
    assert body["status"] == "active"
    assert body["low_balance_threshold"] == 500.0


@pytest.mark.asyncio
async def test_list_transactions_returns_paginated_results(auth_client, db_session, verify_merchant):
    from decimal import Decimal
    from app.models.wallet import Transaction
    import uuid as uuid_mod

    r = await auth_client.post(
        "/api/v1/merchants/", json={"legal_name": "Txn List", "display_name": "TL"}
    )
    mid = r.json()["id"]
    await verify_merchant(mid)

    for i in range(5):
        db_session.add(
            Transaction(
                merchant_id=uuid_mod.UUID(mid),
                amount=Decimal(str(100 * (i + 1))),
                status="successful" if i % 2 == 0 else "failed",
                gateway_ref=f"pay_{i:04d}",
                razorpay_order_id=f"order_{i:04d}",
            )
        )
    await db_session.commit()

    r = await auth_client.get(
        "/api/v1/merchant/wallet/transactions",
        headers={"X-Merchant-Id": mid},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 5
    assert len(body["items"]) == 5


@pytest.mark.asyncio
async def test_patch_wallet_settings_updates_threshold(auth_client, verify_merchant):
    r = await auth_client.post(
        "/api/v1/merchants/", json={"legal_name": "Threshold Test", "display_name": "TT"}
    )
    mid = r.json()["id"]
    await verify_merchant(mid)

    r = await auth_client.patch(
        "/api/v1/merchant/wallet/settings",
        headers={"X-Merchant-Id": mid},
        json={"low_balance_threshold": 2500},
    )
    assert r.status_code == 200
    assert r.json()["low_balance_threshold"] == 2500.0


@pytest.mark.asyncio
async def test_topup_intent_creates_pending_transaction(auth_client, db_session, verify_merchant):
    from unittest.mock import patch
    from sqlalchemy import select
    from app.models.wallet import Transaction

    r = await auth_client.post(
        "/api/v1/merchants/", json={"legal_name": "Intent Test", "display_name": "IT"}
    )
    mid = r.json()["id"]
    await verify_merchant(mid)

    fake_order = {"id": "order_TESTABC", "amount": 100000, "currency": "INR"}
    with (
        patch("app.routers.wallet.create_order", return_value=fake_order),
        patch("app.routers.wallet._get_razorpay_key_id", return_value="rzp_test_xxx"),
    ):
        r = await auth_client.post(
            "/api/v1/merchant/wallet/topup/intent",
            headers={"X-Merchant-Id": mid},
            json={"amount": 1000},
        )

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["order_id"] == "order_TESTABC"
    assert body["amount"] == 1000
    assert body["razorpay_key_id"] == "rzp_test_xxx"

    res = await db_session.execute(
        select(Transaction).where(Transaction.razorpay_order_id == "order_TESTABC")
    )
    txn = res.scalar_one()
    assert txn.status == "pending"
    assert float(txn.amount) == 1000.0


@pytest.mark.asyncio
async def test_topup_intent_maps_provider_auth_failure_to_401(auth_client, verify_merchant):
    from unittest.mock import patch

    class ProviderAuthError(Exception):
        status_code = 401

    response = await auth_client.post(
        "/api/v1/merchants/",
        json={"legal_name": "Razorpay Auth Test", "display_name": "RAT"},
    )
    merchant_id = response.json()["id"]
    await verify_merchant(merchant_id)

    with patch(
        "app.routers.wallet.create_order",
        side_effect=ProviderAuthError("provider credentials rejected"),
    ):
        response = await auth_client.post(
            "/api/v1/merchant/wallet/topup/intent",
            headers={"X-Merchant-Id": merchant_id},
            json={"amount": 500},
        )

    assert response.status_code == 401
    assert response.json()["detail"] == "Razorpay API authentication failed"


@pytest.mark.asyncio
async def test_topup_intent_hides_provider_error_details(auth_client, verify_merchant):
    from unittest.mock import patch

    response = await auth_client.post(
        "/api/v1/merchants/",
        json={"legal_name": "Razorpay Failure Test", "display_name": "RFT"},
    )
    merchant_id = response.json()["id"]
    await verify_merchant(merchant_id)

    with patch(
        "app.routers.wallet.create_order",
        side_effect=RuntimeError("sensitive provider response"),
    ):
        response = await auth_client.post(
            "/api/v1/merchant/wallet/topup/intent",
            headers={"X-Merchant-Id": merchant_id},
            json={"amount": 500},
        )

    assert response.status_code == 500
    assert response.json()["detail"] == "could not create Razorpay order"


@pytest.mark.asyncio
async def test_topup_confirm_credits_wallet_when_signature_valid(auth_client, db_session, verify_merchant):
    from unittest.mock import patch
    from sqlalchemy import select
    from decimal import Decimal
    import uuid as uuid_mod
    from app.models.wallet import Transaction, Wallet

    r = await auth_client.post(
        "/api/v1/merchants/", json={"legal_name": "Confirm Test", "display_name": "CT"}
    )
    mid = r.json()["id"]
    await verify_merchant(mid)

    # Manually seed a pending transaction
    txn = Transaction(
        merchant_id=uuid_mod.UUID(mid),
        amount=Decimal("1500"),
        status="pending",
        razorpay_order_id="order_CONFIRM",
    )
    db_session.add(txn)
    await db_session.commit()

    with patch("app.routers.wallet.verify_payment_signature", return_value=True):
        r = await auth_client.post(
            "/api/v1/merchant/wallet/topup/confirm",
            headers={"X-Merchant-Id": mid},
            json={
                "order_id": "order_CONFIRM",
                "payment_id": "pay_CONFIRM",
                "signature": "irrelevant_in_mock",
            },
        )

    assert r.status_code == 200, r.text

    await db_session.refresh(txn)
    assert txn.status == "successful"
    assert txn.gateway_ref == "pay_CONFIRM"

    res = await db_session.execute(select(Wallet).where(Wallet.merchant_id == uuid_mod.UUID(mid)))
    wallet = res.scalar_one()
    assert float(wallet.balance) == 1500.0


@pytest.mark.asyncio
async def test_topup_confirm_idempotent_returns_existing(auth_client, db_session, verify_merchant):
    from unittest.mock import patch
    from sqlalchemy import select
    from decimal import Decimal
    import uuid as uuid_mod
    from app.models.wallet import Transaction, Wallet

    r = await auth_client.post(
        "/api/v1/merchants/", json={"legal_name": "Idem Test", "display_name": "IT2"}
    )
    mid = r.json()["id"]
    await verify_merchant(mid)

    # Seed an already-credited transaction
    txn = Transaction(
        merchant_id=uuid_mod.UUID(mid),
        amount=Decimal("500"),
        status="successful",
        razorpay_order_id="order_IDEM",
        gateway_ref="pay_IDEM",
    )
    db_session.add(txn)
    res = await db_session.execute(select(Wallet).where(Wallet.merchant_id == uuid_mod.UUID(mid)))
    wallet = res.scalar_one()
    wallet.balance = Decimal("500")
    await db_session.commit()

    # Call confirm again — should be a no-op (already successful)
    with patch("app.routers.wallet.verify_payment_signature", return_value=True):
        r = await auth_client.post(
            "/api/v1/merchant/wallet/topup/confirm",
            headers={"X-Merchant-Id": mid},
            json={
                "order_id": "order_IDEM",
                "payment_id": "pay_IDEM",
                "signature": "x",
            },
        )

    assert r.status_code == 200
    # Balance NOT double-credited
    await db_session.refresh(wallet)
    assert float(wallet.balance) == 500.0


@pytest.mark.asyncio
async def test_topup_confirm_bad_signature_returns_400(auth_client, db_session, verify_merchant):
    from unittest.mock import patch
    from decimal import Decimal
    import uuid as uuid_mod
    from app.models.wallet import Transaction

    r = await auth_client.post(
        "/api/v1/merchants/", json={"legal_name": "Bad Sig", "display_name": "BS"}
    )
    mid = r.json()["id"]
    await verify_merchant(mid)

    txn = Transaction(
        merchant_id=uuid_mod.UUID(mid),
        amount=Decimal("100"),
        status="pending",
        razorpay_order_id="order_BAD",
    )
    db_session.add(txn)
    await db_session.commit()

    with patch("app.routers.wallet.verify_payment_signature", return_value=False):
        r = await auth_client.post(
            "/api/v1/merchant/wallet/topup/confirm",
            headers={"X-Merchant-Id": mid},
            json={
                "order_id": "order_BAD",
                "payment_id": "pay_BAD",
                "signature": "tampered",
            },
        )

    assert r.status_code == 400


@pytest.mark.asyncio
async def test_publish_product_blocks_when_wallet_below_threshold(auth_client, db_session, verify_merchant):
    from sqlalchemy import select
    from decimal import Decimal
    import uuid as uuid_mod
    from app.models.wallet import Wallet

    r = await auth_client.post(
        "/api/v1/merchants/",
        json={
            "legal_name": "Publish Gate",
            "display_name": "PG",
            "settings": {"onboarding_completed": True},
        },
    )
    mid = r.json()["id"]
    await verify_merchant(mid)

    # Wallet starts at 0 (below default threshold of 500)
    r = await auth_client.post(
        "/api/v1/merchant/products/",
        headers={"X-Merchant-Id": mid},
        json={"sku": "GATE-1", "title": "Gated Product"},
    )
    pid = r.json()["id"]

    r = await auth_client.post(
        f"/api/v1/merchant/products/{pid}/publish",
        headers={"X-Merchant-Id": mid},
    )
    assert r.status_code == 402  # Payment Required
    assert "wallet" in r.json()["detail"].lower()

    # Top up the wallet manually and try again
    res = await db_session.execute(select(Wallet).where(Wallet.merchant_id == uuid_mod.UUID(mid)))
    wallet = res.scalar_one()
    wallet.balance = Decimal("600")
    await db_session.commit()

    r = await auth_client.post(
        f"/api/v1/merchant/products/{pid}/publish",
        headers={"X-Merchant-Id": mid},
    )
    assert r.status_code == 200
    assert r.json()["status"] == "published"


@pytest.mark.asyncio
async def test_balance_history_unified_and_filtered(auth_client, db_session, verify_merchant):
    import uuid as uuid_mod
    from decimal import Decimal
    from datetime import datetime, timedelta, timezone
    from sqlalchemy import select
    from app.models.wallet import Wallet, Transaction
    from app.models.event import LedgerEntry, BuyerEvent
    from app.models.merchant_product import MerchantProduct

    now = datetime.now(timezone.utc)
    r = await auth_client.post(
        "/api/v1/merchants/", json={"legal_name": "History Test", "display_name": "HT"}
    )
    mid = r.json()["id"]
    await verify_merchant(mid)
    m_uuid = uuid_mod.UUID(mid)

    # 1. Create a product
    product = MerchantProduct(
        merchant_id=m_uuid,
        sku="TEST-SKU-1",
        title="Test Product",
        in_app_price=100.00,
        status="published"
    )
    db_session.add(product)
    await db_session.flush()

    # 2. Add wallet balance and seeding transactions/ledgers
    res = await db_session.execute(select(Wallet).where(Wallet.merchant_id == m_uuid))
    wallet = res.scalar_one()
    wallet.balance = Decimal("700.00")

    # Add successful transaction (+1000)
    tx = Transaction(
        merchant_id=m_uuid,
        amount=Decimal("1000.00"),
        status="successful",
        payment_method="UPI",
        gateway="razorpay",
        gateway_ref="ref_100",
        razorpay_order_id="order_100",
        created_at=now - timedelta(hours=2)
    )
    db_session.add(tx)
    await db_session.flush()

    # Add click buyer event
    click_event = BuyerEvent(
        user_id=m_uuid,  # dummy user
        merchant_id=m_uuid,
        merchant_product_id=product.id,
        event_type="click",
        billed=True,
        created_at=now - timedelta(hours=1)
    )
    db_session.add(click_event)
    await db_session.flush()

    # Add click ledger deduction (-100)
    le1 = LedgerEntry(
        merchant_id=m_uuid,
        wallet_id=wallet.id,
        related_event_id=click_event.id,
        entry_type="deduction",
        amount=Decimal("-100.00"),
        reason="click",
        balance_after=Decimal("900.00"),
        created_at=now - timedelta(hours=1)
    )
    db_session.add(le1)

    # Add rag mention buyer event
    rag_event = BuyerEvent(
        user_id=m_uuid,
        merchant_id=m_uuid,
        merchant_product_id=product.id,
        event_type="ai_rag_mention",
        billed=True,
        created_at=now
    )
    db_session.add(rag_event)
    await db_session.flush()

    # Add rag ledger deduction (-200)
    le2 = LedgerEntry(
        merchant_id=m_uuid,
        wallet_id=wallet.id,
        related_event_id=rag_event.id,
        entry_type="deduction",
        amount=Decimal("-200.00"),
        reason="ai_rag_mention",
        balance_after=Decimal("700.00"),
        created_at=now
    )
    db_session.add(le2)
    await db_session.commit()

    # Test GET /balance-history
    r = await auth_client.get(
        "/api/v1/merchant/wallet/balance-history",
        headers={"X-Merchant-Id": mid},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 3
    assert len(body["items"]) == 3

    # Check order: newest first (Mention, then Add to Cart, then Deposit)
    items = body["items"]
    assert items[0]["entry_type"] == "Mention"
    assert items[0]["running_balance"] == 700.0
    assert items[0]["product"]["sku"] == "TEST-SKU-1"

    assert items[1]["entry_type"] == "Add to Cart"
    assert items[1]["running_balance"] == 900.0

    assert items[2]["entry_type"] == "Deposit"
    assert items[2]["running_balance"] == 1000.0
    assert items[2]["gateway"] == "razorpay"
    assert items[2]["gateway_ref"] == "ref_100"
    assert items[2]["payment_order_id"] == "order_100"
    assert items[2]["reference_id"] == str(tx.id)
    assert items[2]["order"] is None

    # Test filtering by event_filter=topups
    r = await auth_client.get(
        "/api/v1/merchant/wallet/balance-history?event_filter=topups",
        headers={"X-Merchant-Id": mid},
    )
    assert r.status_code == 200
    assert r.json()["total"] == 1
    assert r.json()["items"][0]["entry_type"] == "Deposit"


@pytest.mark.asyncio
@pytest.mark.parametrize("linked", [True, False])
async def test_balance_history_deduplicates_kyc_transaction_and_ledger(
    auth_client, db_session, verify_merchant, linked
):
    import uuid as uuid_mod
    from decimal import Decimal
    from sqlalchemy import select
    from app.models.wallet import Wallet, Transaction
    from app.models.event import LedgerEntry

    response = await auth_client.post(
        "/api/v1/merchants/",
        json={"legal_name": "KYC History Test", "display_name": "KYC History"},
    )
    merchant_id = response.json()["id"]
    await verify_merchant(merchant_id)
    merchant_uuid = uuid_mod.UUID(merchant_id)

    wallet = (
        await db_session.execute(
            select(Wallet).where(Wallet.merchant_id == merchant_uuid)
        )
    ).scalar_one()
    wallet.balance = Decimal("1000.00")

    transaction = Transaction(
        merchant_id=merchant_uuid,
        amount=Decimal("1000.00"),
        status="successful",
        payment_method="kyc_bonus",
        gateway="system",
        gateway_ref=f"kyc-{uuid_mod.uuid4()}",
    )
    db_session.add(transaction)
    await db_session.flush()
    db_session.add(
        LedgerEntry(
            merchant_id=merchant_uuid,
            wallet_id=wallet.id,
            related_txn_id=transaction.id if linked else None,
            entry_type="credit",
            amount=Decimal("1000.00"),
            reason="kyc_welcome_bonus",
            balance_after=Decimal("1000.00"),
        )
    )
    await db_session.commit()

    response = await auth_client.get(
        "/api/v1/merchant/wallet/balance-history",
        headers={"X-Merchant-Id": merchant_id},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert len(body["items"]) == 1
    assert body["items"][0]["id"] == f"tx_{transaction.id}"
    assert body["items"][0]["payment_method"] == "kyc_bonus"
    assert body["items"][0]["running_balance"] == 1000.0


@pytest.mark.asyncio
async def test_redeem_promo_code_and_referral_code(auth_client, db_session, verify_merchant):
    import uuid as uuid_mod
    from sqlalchemy import select
    from app.models.wallet import Wallet
    from app.models.merchant import Merchant
    from app.models.user import User
    from app.core.security import hash_password, create_access_token
    from httpx import ASGITransport, AsyncClient
    from app.main import app

    # Create Merchant A (owned by test_user)
    r1 = await auth_client.post(
        "/api/v1/merchants/", json={"legal_name": "Merchant A", "display_name": "Merchant A"}
    )
    mid_a = r1.json()["id"]
    await verify_merchant(mid_a)

    # Create user_2 for Merchant B
    user_2 = User(
        email=f"test-b-{uuid_mod.uuid4().hex[:8]}@example.com",
        hashed_password=hash_password("password123"),
        full_name="Merchant B Owner",
    )
    db_session.add(user_2)
    await db_session.commit()
    await db_session.refresh(user_2)

    # Create auth_client_2 for user_2
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as auth_client_2:
        token_2 = create_access_token(str(user_2.id))
        auth_client_2.headers.update({"Authorization": f"Bearer {token_2}"})

        # Create Merchant B (Referrer)
        r2 = await auth_client_2.post(
            "/api/v1/merchants/", json={"legal_name": "Merchant B", "display_name": "Merchant B"}
        )
        mid_b = r2.json()["id"]
        await verify_merchant(mid_b)
        ref_code_b = r2.json()["referral_code"]

        # 1. Self redemption should fail
        r = await auth_client_2.post(
            "/api/v1/merchant/wallet/redeem",
            headers={"X-Merchant-Id": mid_b},
            json={"code": ref_code_b},
        )
        assert r.status_code == 400
        assert "own referral code" in r.json()["detail"]

        # 2. Valid promo code redemption for Merchant A
        r = await auth_client.post(
            "/api/v1/merchant/wallet/redeem",
            headers={"X-Merchant-Id": mid_a},
            json={"code": "WELCOME500"},
        )
        assert r.status_code == 200
        assert r.json()["credit_amount"] == 500.0
        assert r.json()["balance"] == 500.0

        # 3. Double redemption of promo code should fail
        r = await auth_client.post(
            "/api/v1/merchant/wallet/redeem",
            headers={"X-Merchant-Id": mid_a},
            json={"code": "SIMULA1000"},
        )
        assert r.status_code == 400
        assert "already redeemed" in r.json()["detail"]

        # 4. Merchant B redeems Merchant A's referral code
        ref_code_a = r1.json()["referral_code"]
        r = await auth_client_2.post(
            "/api/v1/merchant/wallet/redeem",
            headers={"X-Merchant-Id": mid_b},
            json={"code": ref_code_a},
        )
        assert r.status_code == 200
        assert r.json()["credit_amount"] == 500.0

        # Verify wallet balances
        # Redeemer (Merchant B) gets +500
        res_b = await db_session.execute(select(Wallet).where(Wallet.merchant_id == uuid_mod.UUID(mid_b)))
        wallet_b = res_b.scalar_one()
        assert float(wallet_b.balance) == 500.0

        # Referrer (Merchant A) gets +500
        res_a = await db_session.execute(select(Wallet).where(Wallet.merchant_id == uuid_mod.UUID(mid_a)))
        wallet_a = res_a.scalar_one()
        # Initial balance: 500 (from WELCOME500) + 500 (from Referral partner bonus) = 1000
        assert float(wallet_a.balance) == 1000.0


