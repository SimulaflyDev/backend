import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select, or_, and_

from app.models.wallet import Transaction, Wallet
from app.models.event import LedgerEntry, BuyerEvent
from app.models.lead import Order
from app.models.merchant_product import MerchantProduct
from app.models.merchant import Merchant, MerchantMember
from app.models.notification import Notification
from app.models.user import User
from app.schemas.wallet import (
    PaginatedTransactions,
    TransactionOut,
    WalletOut,
    WalletSettingsUpdate,
    BalanceHistoryResponse,
    RedeemRequest,
    RedeemResponse,
)
from app.utils.dependencies import DBSession
from app.utils.merchant_context import (
    CurrentMerchantContext,
    get_primary_merchant_id,
    require_verified_merchant,
)


async def _notify_merchant_members(
    db,
    merchant_id: uuid.UUID,
    kind: str,
    title: str,
    summary: str,
    payload: dict,
) -> None:
    """Create a Notification row for every member of the merchant."""
    members_res = await db.execute(
        select(MerchantMember).where(MerchantMember.merchant_id == merchant_id)
    )
    for member in members_res.scalars().all():
        db.add(Notification(
            user_id=member.user_id,
            kind=kind,
            title=title,
            summary=summary,
            payload=payload,
        ))

router = APIRouter(
    prefix="/merchant/wallet",
    tags=["merchant-wallet"],
    dependencies=[Depends(require_verified_merchant)],
)


async def _get_wallet_or_404(db, merchant_id: uuid.UUID) -> Wallet:
    res = await db.execute(select(Wallet).where(Wallet.merchant_id == merchant_id))
    wallet = res.scalar_one_or_none()
    if not wallet:
        wallet = Wallet(merchant_id=merchant_id, balance=Decimal("0.00"))
        db.add(wallet)
        await db.flush()
    return wallet


@router.get("/", response_model=WalletOut)
async def get_wallet(db: DBSession, ctx: CurrentMerchantContext) -> Wallet:
    return await _get_wallet_or_404(db, ctx.merchant.id)


@router.get("/transactions", response_model=PaginatedTransactions)
async def list_transactions(
    db: DBSession,
    ctx: CurrentMerchantContext,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict:
    count_stmt = (
        select(func.count())
        .select_from(Transaction)
        .where(Transaction.merchant_id == ctx.merchant.id)
    )
    total = (await db.execute(count_stmt)).scalar_one()

    stmt = (
        select(Transaction)
        .where(Transaction.merchant_id == ctx.merchant.id)
        .order_by(Transaction.created_at.desc())
        .offset(offset)
        .limit(limit)
    )
    rows = (await db.execute(stmt)).scalars().all()
    return {"items": list(rows), "total": total, "limit": limit, "offset": offset}


@router.patch("/settings", response_model=WalletOut)
async def update_wallet_settings(
    body: WalletSettingsUpdate, db: DBSession, ctx: CurrentMerchantContext
) -> Wallet:
    wallet = await _get_wallet_or_404(db, ctx.merchant.id)

    data = body.model_dump(exclude_unset=True)
    for k, v in data.items():
        setattr(wallet, k, v)
    await db.commit()
    await db.refresh(wallet)
    return wallet


from decimal import Decimal
from datetime import datetime, timezone

from app.core.config import get_settings
from app.schemas.wallet import (
    TopupConfirmRequest,
    TopupIntentRequest,
    TopupIntentResponse,
)
from app.services.razorpay_client import create_order, verify_payment_signature


def _get_razorpay_key_id() -> str:
    """Wrapped for testability."""
    return get_settings().RAZORPAY_KEY_ID


@router.post("/topup/intent", response_model=TopupIntentResponse)
async def topup_intent(
    body: TopupIntentRequest, db: DBSession, ctx: CurrentMerchantContext
) -> dict:
    # Create a pending Transaction first so we have an id for the receipt
    txn = Transaction(
        merchant_id=ctx.merchant.id,
        amount=Decimal(str(body.amount)),
        currency=body.currency,
        status="pending",
    )
    db.add(txn)
    await db.flush()

    try:
        order = create_order(amount_inr=body.amount, receipt=f"txn_{txn.id}")
    except Exception as exc:  # noqa: BLE001
        await db.rollback()
        if getattr(exc, "status_code", None) == 401:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Razorpay API authentication failed",
            ) from exc
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="could not create Razorpay order",
        ) from exc

    if not order.get("id"):
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Razorpay returned an invalid order",
        )

    txn.razorpay_order_id = order["id"]
    await db.commit()
    await db.refresh(txn)

    return {
        "order_id": order["id"],
        "razorpay_key_id": _get_razorpay_key_id(),
        "amount": body.amount,
        "currency": body.currency,
        "transaction_id": txn.id,
    }


@router.post("/topup/confirm", response_model=WalletOut)
async def topup_confirm(
    body: TopupConfirmRequest, db: DBSession, ctx: CurrentMerchantContext
) -> Wallet:
    if not verify_payment_signature(body.order_id, body.payment_id, body.signature):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="invalid signature"
        )

    res = await db.execute(
        select(Transaction).where(
            Transaction.razorpay_order_id == body.order_id,
            Transaction.merchant_id == ctx.merchant.id,
        ).with_for_update()
    )
    txn = res.scalar_one_or_none()
    if not txn:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="transaction not found"
        )

    # Idempotency: if already successful with this gateway_ref, no-op
    if txn.status == "successful" and txn.gateway_ref == body.payment_id:
        return await _get_wallet_or_404(db, ctx.merchant.id)

    if txn.status == "successful":
        # Already credited via webhook with a different ref — return wallet as-is
        return await _get_wallet_or_404(db, ctx.merchant.id)

    # Mark transaction successful + credit wallet atomically
    txn.status = "successful"
    txn.gateway_ref = body.payment_id
    txn.razorpay_signature = body.signature

    wallet = await _get_wallet_or_404(db, ctx.merchant.id)
    wallet.balance = wallet.balance + txn.amount
    wallet.last_recharged_at = datetime.now(timezone.utc)
    if wallet.status == "depleted":
        wallet.status = "active"

    await db.commit()

    # Notify all merchant members about successful payment
    try:
        await _notify_merchant_members(
            db,
            merchant_id=ctx.merchant.id,
            kind="payment",
            title="💰 Payment Received",
            summary=f"₹{float(txn.amount):,.2f} has been added to your wallet successfully.",
            payload={"transaction_id": str(txn.id), "amount": float(txn.amount), "gateway_ref": body.payment_id},
        )
        await db.commit()
    except Exception:
        pass  # Non-critical — don't fail the topup

    await db.refresh(wallet)
    return wallet


@router.get("/balance-history", response_model=BalanceHistoryResponse)
async def get_balance_history(
    db: DBSession,
    ctx: CurrentMerchantContext,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    time_window: str = Query(default="all_time"),
    event_filter: str = Query(default="all"),
) -> dict:
    wallet = await _get_wallet_or_404(db, ctx.merchant.id)
    current_balance = float(wallet.balance)

    # 1. Resolve date range filter
    start_date = None
    now = datetime.now(timezone.utc)
    if time_window == "today":
        start_date = now.replace(hour=0, minute=0, second=0, microsecond=0)
    elif time_window == "7_days":
        start_date = now - timedelta(days=7)
    elif time_window == "30_days":
        start_date = now - timedelta(days=30)

    # 2. Query transactions (deposits)
    txs = []
    if event_filter in ("all", "topups"):
        tx_stmt = select(Transaction).where(
            Transaction.merchant_id == ctx.merchant.id,
            Transaction.status == "successful"
        )
        if start_date:
            tx_stmt = tx_stmt.where(Transaction.created_at >= start_date)
        tx_res = await db.execute(tx_stmt)
        txs = tx_res.scalars().all()

    # 3. Query ledger entries
    ledgers = []
    if event_filter != "topups":
        ledg_stmt = (
            select(
                LedgerEntry,
                BuyerEvent.event_type.label("buyer_event_type"),
                MerchantProduct.title.label("product_title"),
                MerchantProduct.sku.label("product_sku"),
                MerchantProduct.primary_image_url.label("product_image_url"),
                Order,
                User.full_name.label("buyer_name"),
            )
            .outerjoin(BuyerEvent, LedgerEntry.related_event_id == BuyerEvent.id)
            .outerjoin(MerchantProduct, BuyerEvent.merchant_product_id == MerchantProduct.id)
            .outerjoin(Order, LedgerEntry.related_order_id == Order.id)
            .outerjoin(User, Order.user_id == User.id)
            .where(LedgerEntry.merchant_id == ctx.merchant.id)
        )
        if start_date:
            ledg_stmt = ledg_stmt.where(LedgerEntry.created_at >= start_date)

        if event_filter == "add_to_cart":
            ledg_stmt = ledg_stmt.where(BuyerEvent.event_type == "click")
        elif event_filter == "views":
            ledg_stmt = ledg_stmt.where(BuyerEvent.event_type == "impression")
        elif event_filter == "activity":
            ledg_stmt = ledg_stmt.where(BuyerEvent.event_type == "ai_rag_mention")
        elif event_filter == "referrals":
            ledg_stmt = ledg_stmt.where(
                or_(
                    LedgerEntry.reason.like("referral_%"),
                    LedgerEntry.reason.like("promo_%"),
                    LedgerEntry.entry_type == "credit"
                )
            )

        ledg_res = await db.execute(ledg_stmt)
        ledgers = ledg_res.all()

    # 4. Map to unified balance history format
    items = []
    # A balance change can have both a Transaction (payment/audit record) and a
    # LedgerEntry (accounting record). When both are returned, they represent
    # one wallet movement and must render as one history item.
    visible_transaction_ids = {tx.id for tx in txs}
    legacy_kyc_bonus_amounts = {
        tx.amount for tx in txs if tx.payment_method == "kyc_bonus"
    }
    for tx in txs:
        items.append({
            "id": f"tx_{tx.id}",
            "created_at": tx.created_at,
            "amount": float(tx.amount),
            "entry_type": "Deposit",
            "reason": "topup",
            "payment_method": tx.payment_method or "UPI",
            "gateway_ref": tx.gateway_ref,
            "gateway": tx.gateway,
            "payment_order_id": tx.razorpay_order_id,
            "reference_id": str(tx.id),
            "product": None,
            "order": None,
            "running_balance": 0.0,
        })

    for row in ledgers:
        ledg = row[0]
        be_type = row[1]
        p_title = row[2]
        p_sku = row[3]
        p_img = row[4]
        order = row[5]
        buyer_name = row[6]

        if ledg.related_txn_id in visible_transaction_ids:
            continue
        # KYC bonus rows created before related_txn_id was populated still have
        # a matching successful transaction. Suppress that legacy ledger copy.
        if (
            ledg.related_txn_id is None
            and ledg.reason == "kyc_welcome_bonus"
            and ledg.amount in legacy_kyc_bonus_amounts
        ):
            continue

        # Map display name of event type
        if be_type == "ai_rag_mention":
            disp_type = "Mention"
        elif be_type == "click":
            disp_type = "Add to Cart"
        elif be_type == "impression":
            disp_type = "View"
        elif ledg.entry_type == "credit":

            disp_type = "Deposit"
        else:
            disp_type = ledg.reason.replace("_", " ").title()

        prod = None
        if p_title:
            prod = {
                "title": p_title,
                "sku": p_sku,
                "image_url": p_img
            }

        order_details = None
        if order is not None:
            order_items = []
            for item in order.items or []:
                order_items.append({
                    "product_id": str(item.get("product_id")) if item.get("product_id") else None,
                    "title": str(item.get("title") or "Product"),
                    "sku": str(item.get("sku") or "-"),
                    "quantity": int(item.get("qty") or 1),
                })

            address = order.delivery_address or {}
            address_parts = [
                address.get("address_line1"),
                address.get("city"),
                address.get("state"),
                address.get("pincode"),
            ]
            order_details = {
                "id": str(order.id),
                "created_at": order.created_at,
                "accepted_at": order.accepted_at,
                "status": order.status,
                "buyer_name": buyer_name,
                "buyer_address": ", ".join(str(part) for part in address_parts if part),
                "total_value": float(order.total_estimated),
                "items": order_items,
            }

            if prod is None and order_items:
                prod = {
                    "title": order_items[0]["title"],
                    "sku": order_items[0]["sku"],
                    "image_url": None,
                }

        items.append({
            "id": f"le_{ledg.id}",
            "created_at": ledg.created_at,
            "amount": float(ledg.amount),
            "entry_type": disp_type,
            "reason": ledg.reason,
            "payment_method": None,
            "gateway_ref": None,
            "gateway": None,
            "payment_order_id": None,
            "reference_id": str(ledg.related_event_id) if ledg.related_event_id else None,
            "product": prod,
            "order": order_details,
            "running_balance": 0.0,
        })

    # 5. Sort descending
    items.sort(key=lambda x: x["created_at"], reverse=True)

    # 6. Compute running balances going backwards
    running = current_balance
    for i in range(len(items)):
        items[i]["running_balance"] = running
        running = running - items[i]["amount"]

    # 7. Paginate
    total = len(items)
    paginated = items[offset : offset + limit]

    return {
        "items": paginated,
        "total": total,
        "limit": limit,
        "offset": offset
    }


@router.post("/redeem", response_model=RedeemResponse)
async def redeem_code(
    body: RedeemRequest,
    db: DBSession,
    ctx: CurrentMerchantContext,
) -> dict:
    wallet = await _get_wallet_or_404(db, ctx.merchant.id)
    code = body.code.strip().upper()

    # Get the redeemer's merchant object
    redeemer_res = await db.execute(select(Merchant).where(Merchant.id == ctx.merchant.id))
    redeemer = redeemer_res.scalar_one()

    # 1. Prevent self-redemption
    if redeemer.referral_code.upper() == code:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot redeem your own referral code."
        )

    # 2. Check if redeemer has already redeemed a coupon/promo code
    already_redeemed = await db.execute(
        select(LedgerEntry).where(
            LedgerEntry.merchant_id == ctx.merchant.id,
            LedgerEntry.entry_type == "credit",
            or_(
                LedgerEntry.reason == "referral_redeem",
                LedgerEntry.reason.like("promo_%")
            )
        )
    )
    if already_redeemed.scalars().first():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="You have already redeemed a referral or promo code."
        )

    # 3. Check code type
    # Check if promo code first
    promo_amounts = {
        "WELCOME200": 200.0,
        "WELCOME500": 500.0,
        "SIMULA1000": 1000.0,
        "GIFT1000": 1000.0,
        "SIMULAFREE": 1000.0,
    }

    if code in promo_amounts:
        credit_amount = promo_amounts[code]
        wallet.balance = wallet.balance + Decimal(str(credit_amount))
        
        # Write ledger credit
        ledger = LedgerEntry(
            merchant_id=ctx.merchant.id,
            wallet_id=wallet.id,
            entry_type="credit",
            amount=Decimal(str(credit_amount)),
            reason=f"promo_{code.lower()}",
            balance_after=wallet.balance,
            notes=f"Promo code {code} applied"
        )
        db.add(ledger)
        await db.commit()

        # Notify merchant members about promo code credit
        try:
            await _notify_merchant_members(
                db,
                merchant_id=ctx.merchant.id,
                kind="wallet",
                title="🎁 Promo Code Applied",
                summary=f"Promo code {code} applied! ₹{credit_amount:,.2f} credited to your wallet.",
                payload={"code": code, "credit_amount": credit_amount, "reason": f"promo_{code.lower()}"},
            )
            await db.commit()
        except Exception:
            pass

        await db.refresh(wallet)
        return {
            "message": f"Promo code applied successfully! ₹{credit_amount:,.2f} added to wallet.",
            "balance": float(wallet.balance),
            "credit_amount": credit_amount
        }

    # If not a promo code, check if it is another merchant's referral code
    ref_res = await db.execute(select(Merchant).where(func.upper(Merchant.referral_code) == code))
    referrer = ref_res.scalar_one_or_none()

    if not referrer:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid or expired referral or promo code."
        )

    # Valid referral code of another merchant:
    # Redeemer gets ₹500
    redeemer_amount = 500.0
    wallet.balance = wallet.balance + Decimal(str(redeemer_amount))
    
    redeemer_ledger = LedgerEntry(
        merchant_id=ctx.merchant.id,
        wallet_id=wallet.id,
        entry_type="credit",
        amount=Decimal(str(redeemer_amount)),
        reason="referral_redeem",
        balance_after=wallet.balance,
        notes=f"Referred by {referrer.display_name} ({referrer.referral_code})"
    )
    db.add(redeemer_ledger)

    # Referrer gets ₹500
    referrer_amount = 500.0
    ref_wallet_res = await db.execute(select(Wallet).where(Wallet.merchant_id == referrer.id))
    ref_wallet = ref_wallet_res.scalar_one_or_none()
    if not ref_wallet:
        ref_wallet = Wallet(merchant_id=referrer.id, balance=Decimal("0.00"))
        db.add(ref_wallet)
        await db.flush()

    if ref_wallet:
        ref_wallet.balance = ref_wallet.balance + Decimal(str(referrer_amount))
        referrer_ledger = LedgerEntry(
            merchant_id=referrer.id,
            wallet_id=ref_wallet.id,
            entry_type="credit",
            amount=Decimal(str(referrer_amount)),
            reason="referral_partner",
            balance_after=ref_wallet.balance,
            notes=f"Referral reward from {redeemer.display_name}"
        )
        db.add(referrer_ledger)

    await db.commit()

    # Notify redeemer
    try:
        await _notify_merchant_members(
            db,
            merchant_id=ctx.merchant.id,
            kind="wallet",
            title="🤝 Referral Code Applied",
            summary=f"Referral code accepted! ₹{redeemer_amount:,.2f} credited to your wallet.",
            payload={"code": code, "credit_amount": redeemer_amount, "reason": "referral_redeem"},
        )
        await db.commit()
    except Exception:
        pass

    # Notify referrer about the bonus they earned
    if ref_wallet:
        try:
            await _notify_merchant_members(
                db,
                merchant_id=referrer.id,
                kind="wallet",
                title="🎉 Referral Bonus Earned!",
                summary=f"{redeemer.display_name} used your referral code! ₹{referrer_amount:,.2f} has been credited to your wallet.",
                payload={"referral_code": code, "credit_amount": referrer_amount, "redeemer": redeemer.display_name, "reason": "referral_partner"},
            )
            await db.commit()
        except Exception:
            pass

    await db.refresh(wallet)
    return {
        "message": f"Referral code accepted! ₹{redeemer_amount:,.2f} credited to your wallet, and ₹{referrer_amount:,.2f} to {referrer.display_name}.",
        "balance": float(wallet.balance),
        "credit_amount": redeemer_amount
    }


