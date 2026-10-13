"""Central billing engine — records buyer events, deducts wallet, pauses products.

BillingService orchestrates the full event-billing pipeline:

  1. Insert BuyerEvent row (always, even if not billable)
  2. If event_type is dedupable AND already seen this hour: billed=False, exit
  3. Resolve rate via PricingService; if 0: billed=False, exit
  4. Atomically: insert LedgerEntry, decrement Wallet.balance
  5. Caller schedules pause_if_depleted_for(merchant_id) as a BackgroundTask

Pause logic is eventually-consistent per spec §2.4.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.models.event import BuyerEvent, BuyerEventDedup, LedgerEntry
from app.models.merchant import MerchantMember
from app.models.merchant_product import MerchantProduct
from app.models.notification import Notification
from app.models.wallet import Wallet
from app.services.dedup import _current_hour_bucket, is_dedupable
from app.services.pricing import resolve_rate

log = get_logger("app.services.billing")


class BillingService:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def _check_dedup(
        self,
        *,
        event_type: str,
        session_id: str,
        product_id: uuid.UUID,
    ) -> bool:
        """Return True if billable (first occurrence). Uses a savepoint so
        IntegrityError on duplicate only rolls back the savepoint, not the
        surrounding transaction."""
        row = BuyerEventDedup(
            event_type=event_type,
            user_session_id=session_id,
            merchant_product_id=product_id,
            hour_bucket=_current_hour_bucket(),
        )
        try:
            async with self.db.begin_nested():
                self.db.add(row)
                await self.db.flush()
        except IntegrityError:
            return False
        return True

    async def record_event(
        self,
        *,
        event_type: str,
        user_id: uuid.UUID,
        merchant_id: uuid.UUID,
        product_id: uuid.UUID | None,
        session_id: str | None,
        context: dict,
        client_ip: str | None = None,
    ) -> BuyerEvent:
        """Record a buyer event; deduct wallet if billable."""
        # 1. Check dedup (only for dedupable event types)
        billable = True
        if product_id is not None and session_id is not None and is_dedupable(event_type):
            billable = await self._check_dedup(
                event_type=event_type,
                session_id=session_id,
                product_id=product_id,
            )

        # 2. Resolve rate
        rate = Decimal("0")
        if billable:
            rate, _ = await resolve_rate(self.db, event_type, merchant_id)

        will_deduct = billable and rate > 0

        # 3. Insert BuyerEvent
        event = BuyerEvent(
            user_id=user_id,
            merchant_id=merchant_id,
            merchant_product_id=product_id,
            event_type=event_type,
            context=context,
            user_session_id=session_id,
            client_ip=client_ip,
            billed=will_deduct,
        )
        self.db.add(event)
        await self.db.flush()

        # 4. If deducting, write LedgerEntry + decrement Wallet
        if will_deduct:
            await self._deduct(
                merchant_id=merchant_id,
                amount=rate,
                reason=event_type,
                related_event_id=event.id,
            )

        await self.db.commit()
        await self.db.refresh(event)
        return event

    async def _deduct(
        self,
        *,
        merchant_id: uuid.UUID,
        amount: Decimal,
        reason: str,
        related_event_id: uuid.UUID | None = None,
        related_order_id: uuid.UUID | None = None,
    ) -> None:
        """Deduct `amount` from the merchant's wallet and write a LedgerEntry."""
        res = await self.db.execute(select(Wallet).where(Wallet.merchant_id == merchant_id))
        wallet = res.scalar_one_or_none()
        if not wallet:
            log.warning("billing_no_wallet", merchant_id=str(merchant_id))
            return

        wallet.balance = wallet.balance - amount
        ledger = LedgerEntry(
            merchant_id=merchant_id,
            wallet_id=wallet.id,
            related_event_id=related_event_id,
            related_order_id=related_order_id,
            entry_type="deduction",
            amount=-amount,
            reason=reason,
            balance_after=wallet.balance,
        )
        self.db.add(ledger)
        await self.db.flush()

    async def transaction_fee_on_acceptance(self, *, order: "Order") -> Decimal:
        """Charge the admin-configured purchase fee once when an order is accepted."""
        if order.fee_charged_at is not None:
            return order.platform_fee_amount

        rate, rate_type = await resolve_rate(
            self.db,
            "simulafly_purchase",
            order.merchant_id,
        )
        amount = rate
        if rate_type == "percentage":
            amount = (order.total_estimated * rate / Decimal("100")).quantize(
                Decimal("0.0001")
            )

        if amount > 0:
            await self._deduct(
                merchant_id=order.merchant_id,
                amount=amount,
                reason="order_confirmation",
                related_order_id=order.id,
            )

        order.platform_fee_amount = amount
        order.fee_charged_at = datetime.now(timezone.utc)
        await self.db.flush()
        return amount

    async def pause_if_depleted_for(self, merchant_id: uuid.UUID) -> None:
        """Eventual pause check (called from BackgroundTasks after each deduction)."""
        res = await self.db.execute(select(Wallet).where(Wallet.merchant_id == merchant_id))
        wallet = res.scalar_one_or_none()
        if not wallet or wallet.balance > 0:
            return

        wallet.status = "depleted"
        await self.db.execute(
            update(MerchantProduct)
            .where(
                MerchantProduct.merchant_id == merchant_id,
                MerchantProduct.status == "published",
            )
            .values(status="paused_insufficient_funds")
        )
        await self.db.commit()

        # Notify all merchant members about depletion
        try:
            members_res = await self.db.execute(
                select(MerchantMember).where(MerchantMember.merchant_id == merchant_id)
            )
            for member in members_res.scalars().all():
                self.db.add(Notification(
                    user_id=member.user_id,
                    kind="wallet",
                    title="⚠️ Wallet Depleted — Products Paused",
                    summary="Your wallet balance has reached ₹0. All your products have been paused. Please top up to resume visibility.",
                    payload={"merchant_id": str(merchant_id), "action": "paused"},
                ))
            await self.db.commit()
        except Exception:
            pass  # Non-critical

        log.info("billing_pause_depleted", merchant_id=str(merchant_id))

    async def pause_if_depleted_all(self) -> int:
        """Safety-net sweep: pause all depleted merchants. Called from cron.

        Returns the number of merchants paused.
        """
        res = await self.db.execute(
            select(Wallet).where(Wallet.balance <= 0, Wallet.status != "depleted")
        )
        depleted_wallets = list(res.scalars().all())
        for w in depleted_wallets:
            await self.pause_if_depleted_for(w.merchant_id)
        return len(depleted_wallets)
