"""Merchant-facing lead history and authoritative order progress updates."""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.lead import BuyerLead, LeadStatus, Order
from app.models.merchant import Merchant
from app.models.user import User
from app.schemas.lead import BuyerLeadOut, BuyerLeadUpdate, CustomerInfo, OrderMerchantOut, OrderOut, PaginatedLeads
from app.services.order_lifecycle import apply_order_update
from app.utils.dependencies import DBSession
from app.utils.merchant_context import CurrentMerchantContext, require_verified_merchant

router = APIRouter(prefix="/merchant/leads", tags=["merchant-leads"], dependencies=[Depends(require_verified_merchant)])


async def _build_lead_out(lead: BuyerLead, db: AsyncSession, reveal_pii: bool) -> BuyerLeadOut:
    user = await db.get(User, lead.user_id)
    order = (await db.execute(select(Order).where(Order.lead_id == lead.id))).scalar_one_or_none()
    merchant = await db.get(Merchant, lead.merchant_id)
    addr = order.delivery_address if order else {}
    customer = CustomerInfo(city=addr.get("city") or lead.delivery_city)
    if reveal_pii and user:
        customer = CustomerInfo(
            city=addr.get("city") or lead.delivery_city, name=user.full_name, email=user.email,
            phone=addr.get("phone") or lead.delivery_phone,
            address_line1=addr.get("address_line1") or user.address_line1,
            state=addr.get("state") or user.state, pincode=addr.get("pincode") or user.pincode,
            latitude=addr.get("latitude"), longitude=addr.get("longitude"),
        )
    order_out = OrderOut.model_validate(order) if order else None
    # Do not leak the newly exposed delivery snapshot before acceptance.
    if order_out and not reveal_pii:
        order_out.delivery_address = {}
    snapshot = order.merchant_snapshot if order else None
    return BuyerLeadOut(
        id=lead.id, merchant_id=lead.merchant_id, lead_type=lead.lead_type, status=lead.status,
        estimated_value=lead.estimated_value, ai_interactions_count=lead.ai_interactions_count,
        ai_generated_image_url=lead.ai_generated_image_url, delivery_city=lead.delivery_city,
        merchant_notes=lead.merchant_notes, cancellation_reason=lead.cancellation_reason,
        converted_at=lead.converted_at, created_at=lead.created_at, updated_at=lead.updated_at,
        customer=customer, order=order_out,
        merchant=OrderMerchantOut.model_validate(snapshot or merchant) if (snapshot or merchant) else None,
    )


@router.get("/", response_model=PaginatedLeads)
async def list_leads(
    ctx: CurrentMerchantContext, db: DBSession,
    lead_status: str | None = Query(default=None, alias="status"), lead_type: str | None = None,
    limit: int = Query(default=25, ge=1, le=100), offset: int = Query(default=0, ge=0),
):
    q = select(BuyerLead).where(BuyerLead.merchant_id == ctx.merchant.id)
    if lead_status:
        q = q.where(BuyerLead.status == lead_status)
    if lead_type:
        q = q.where(BuyerLead.lead_type == lead_type)
    total = (await db.execute(select(func.count()).select_from(q.subquery()))).scalar_one()
    leads = (await db.execute(q.order_by(BuyerLead.created_at.desc()).limit(limit).offset(offset))).scalars().all()
    return PaginatedLeads(items=[await _build_lead_out(lead, db, lead.status not in {"new", "lost"}) for lead in leads], total=total, limit=limit, offset=offset)


@router.get("/{lead_id}", response_model=BuyerLeadOut)
async def get_lead(lead_id: uuid.UUID, ctx: CurrentMerchantContext, db: DBSession):
    lead = await db.get(BuyerLead, lead_id)
    if not lead or lead.merchant_id != ctx.merchant.id:
        raise HTTPException(404, "lead not found")
    return await _build_lead_out(lead, db, lead.status not in {"new", "lost"})


@router.patch("/{lead_id}", response_model=BuyerLeadOut)
async def update_lead(lead_id: uuid.UUID, body: BuyerLeadUpdate, ctx: CurrentMerchantContext, db: DBSession):
    # Lock the order first everywhere, including admin transitions.
    order = (await db.execute(select(Order).where(
        Order.lead_id == lead_id, Order.merchant_id == ctx.merchant.id,
    ).with_for_update())).scalar_one_or_none()
    lead = (await db.execute(select(BuyerLead).where(
        BuyerLead.id == lead_id, BuyerLead.merchant_id == ctx.merchant.id,
    ).with_for_update())).scalar_one_or_none()
    if not lead:
        raise HTTPException(404, "lead not found")
    if order:
        await apply_order_update(db, order, lead, lead_status=body.status,
            fulfillment_status=body.fulfillment_status, payment_status=body.payment_status)
    elif body.fulfillment_status or body.payment_status:
        raise HTTPException(409, "This lead does not have an order")
    elif body.status:
        if body.status not in {s.value for s in LeadStatus}:
            raise HTTPException(422, "invalid lead status")
        lead.status = body.status
    if body.cancellation_reason and lead.status == "lost":
        lead.cancellation_reason = body.cancellation_reason.model_dump()
    if body.merchant_notes is not None:
        lead.merchant_notes = body.merchant_notes
    await db.commit()
    await db.refresh(lead)
    return await _build_lead_out(lead, db, lead.status not in {"new", "lost"})
