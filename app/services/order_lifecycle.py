"""Order transitions. Callers must lock the order before calling and commit once.

Payment is a merchant receipt confirmation, NOT a payment gateway charge.
Legacy `converted` means payment received; it never implies delivery.
"""
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import func, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.lead import BuyerLead, Order
from app.models.notification import Notification
from app.models.user import User
from app.services.billing import BillingService

STAGES = ["pending", "in_progress", "shipped", "out_for_delivery", "fulfilled"]


async def apply_order_update(
    db: AsyncSession, order: Order, lead: BuyerLead, *,
    lead_status: str | None = None,
    fulfillment_status: str | None = None,
    payment_status: str | None = None,
) -> None:
    if order.deleted_at is not None:
        raise HTTPException(404, "order not found")
    if lead_status not in {None, "new", "synced", "converted", "lost"}:
        raise HTTPException(422, "invalid lead status")
    if fulfillment_status not in {None, *STAGES, "cancelled"}:
        raise HTTPException(422, "invalid fulfillment status")
    if payment_status not in {None, "paid"}:
        raise HTTPException(422, "invalid payment status")

    # Old completed orders are deliberately terminal: no retroactive rewards
    # and no invented payment/delivery timestamps during migration or retries.
    if order.status in {"completed", "cancelled"} or order.fulfillment_status == "cancelled":
        repeat = (
            lead_status in {None, lead.status}
            and fulfillment_status in {None, order.fulfillment_status}
            and payment_status in {None, order.payment_status}
        )
        if repeat:
            return
        raise HTTPException(409, "A completed or cancelled order cannot be reopened")

    cancelling = lead_status == "lost" or fulfillment_status == "cancelled"
    if cancelling:
        if payment_status or lead_status == "converted" or order.fulfillment_status == "fulfilled":
            raise HTTPException(409, "Delivery/payment and cancellation cannot be combined")
        order.status = "cancelled"
        order.fulfillment_status = "cancelled"
        lead.status = "lost"
        db.add(Notification(user_id=lead.user_id, kind="delivery", title="Order cancelled",
            summary="The merchant cancelled your order. Contact the shop if payment was already made.",
            payload={"lead_id": str(lead.id), "order_id": str(order.id)}))
        return

    if lead_status == "new" and order.status != "pending_merchant_contact":
        raise HTTPException(409, "An accepted order cannot return to placed")
    if fulfillment_status and STAGES.index(fulfillment_status) < STAGES.index(order.fulfillment_status):
        raise HTTPException(409, "Delivery progress cannot move backwards")
    if lead_status == "new" and (payment_status or fulfillment_status not in {None, "pending"}):
        raise HTTPException(409, "Conflicting order progress")
    if fulfillment_status == "pending" and (payment_status or lead_status in {"synced", "converted"}):
        raise HTTPException(409, "An accepted order cannot return to pending fulfillment")

    now = datetime.now(timezone.utc)
    before = (order.status, order.fulfillment_status, order.payment_status)
    advancing = lead_status in {"synced", "converted"} or payment_status or fulfillment_status not in {None, "pending"}
    if advancing and order.accepted_at is None:
        order.accepted_at = now
        order.status = "contacted"
        if order.fulfillment_status == "pending":
            order.fulfillment_status = "in_progress"
        await BillingService(db).transaction_fee_on_acceptance(order=order)
    if advancing:
        lead.status = "synced"
    if fulfillment_status:
        order.fulfillment_status = fulfillment_status
        timestamp = {"shipped": "shipped_at", "out_for_delivery": "out_for_delivery_at", "fulfilled": "delivered_at"}.get(fulfillment_status)
        if timestamp and getattr(order, timestamp) is None:
            setattr(order, timestamp, now)
    if payment_status == "paid" or lead_status == "converted":
        order.payment_status = "paid"
        order.payment_completed_at = order.payment_completed_at or now

    if order.fulfillment_status == "fulfilled" and order.payment_status == "paid":
        order.status = "completed"
        order.completed_at = now
        lead.status = "converted"
        lead.converted_at = now
        # Atomic guard + order row lock: retries/concurrent PATCHes cannot mint
        # multiple rewards. Increment in SQL also protects different orders for
        # the same buyer being completed concurrently.
        await db.flush()
        awarded = await db.execute(update(Order).where(
            Order.id == order.id, Order.reward_granted_at.is_(None), Order.reward_tokens == 0,
        ).values(reward_granted_at=now, reward_tokens=10))
        if awarded.rowcount:
            await db.execute(update(User).where(User.id == order.user_id).values(
                credit_balance=func.coalesce(User.credit_balance, 0) + 10,
            ))
            db.add(Notification(user_id=lead.user_id, kind="delivery", title="Order complete · +10 tokens",
                summary="Delivery and payment are complete. 10 tokens have been added to your balance.",
                payload={"lead_id": str(lead.id), "order_id": str(order.id), "reward_tokens": 10}))
    elif before != (order.status, order.fulfillment_status, order.payment_status):
        label = "Delivered — awaiting payment confirmation" if order.fulfillment_status == "fulfilled" else order.fulfillment_status.replace("_", " ").title()
        db.add(Notification(user_id=lead.user_id, kind="delivery", title="Order progress updated",
            summary=f"{label}. Payment: {order.payment_status}.",
            payload={"lead_id": str(lead.id), "order_id": str(order.id)}))
