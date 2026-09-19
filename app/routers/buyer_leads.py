"""Buyer-facing lead submission router (Flutter → 'Buy on SimulaFly').

POST /buyer/leads/     -- submit a purchase-intent lead
GET  /buyer/leads/me   -- buyer's own lead history
"""
from __future__ import annotations

from decimal import Decimal

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from app.models.event import BuyerEvent
from app.models.lead import BuyerLead, LeadStatus, LeadType, Order, OrderStatus
from app.models.merchant_product import MerchantProduct, MerchantProductVariant
from app.models.merchant import Merchant
from app.schemas.lead import (
    BuyerLeadCreate,
    BuyerLeadOut,
    CustomerInfo,
    OrderOut,
    OrderMerchantOut,
    PaginatedLeads,
)
from app.services.coupons import check_coupon
from app.utils.dependencies import CurrentUser, DBSession

router = APIRouter(prefix="/buyer/leads", tags=["buyer-leads"])


@router.post("/", response_model=BuyerLeadOut, status_code=status.HTTP_201_CREATED)
async def submit_lead(
    body: BuyerLeadCreate,
    user: CurrentUser,
    db: DBSession,
):
    stmt = (
        select(MerchantProduct)
        .options(selectinload(MerchantProduct.merchant))
        .where(MerchantProduct.id == body.merchant_product_id)
    )
    product = (await db.execute(stmt)).scalar_one_or_none()
    if not product or product.status != "published":
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="product not found or unavailable",
        )

    # Count AI interactions for this buyer+product (best-effort)
    count_res = await db.execute(
        select(func.count()).where(
            BuyerEvent.user_id == user.id,
            BuyerEvent.merchant_product_id == product.id,
        )
    )
    ai_count: int = count_res.scalar_one()

    # Build items list
    if body.items:
        products = (await db.execute(select(MerchantProduct).where(
            MerchantProduct.id.in_([i.product_id for i in body.items]),
        ))).scalars().all()
        by_id = {p.id: p for p in products}
        if product.id not in {i.product_id for i in body.items}:
            raise HTTPException(422, "Main product must be included in the order")
        items_payload = []
        subtotal = Decimal("0")
        for item in body.items:
            p = by_id.get(item.product_id)
            if not p or p.status != "published" or p.merchant_id != product.merchant_id:
                raise HTTPException(422, "All order items must be available from the same shop")
            if p.in_app_price is None:
                raise HTTPException(422, "Product price is unavailable")
            price = Decimal(str(p.in_app_price))
            variant = None
            if item.variant_id:
                variant = await db.get(MerchantProductVariant, item.variant_id)
                if not variant or variant.merchant_product_id != p.id:
                    raise HTTPException(422, "Invalid product variant")
                price += Decimal(str(variant.price_modifier))
            if price < 0:
                raise HTTPException(422, "Invalid product price")
            items_payload.append({
                "product_id": str(p.id), "variant_id": str(item.variant_id) if variant else None,
                "qty": item.qty, "price_at_capture": float(price),
                "title": f"{p.title} — {variant.label}" if variant else p.title,
                "img_url": (variant.primary_image_url if variant else None) or p.primary_image_url,
                "sku": f"{p.sku}-{variant.sku_suffix}" if variant else p.sku,
            })
            subtotal += price * item.qty
    else:
        # Fallback: single unit of the main product
        if product.in_app_price is None or product.in_app_price < 0:
            raise HTTPException(422, "Product price is unavailable")
        items_payload = [
            {
                "product_id": str(product.id),
                "variant_id": None,
                "qty": 1,
                "price_at_capture": float(product.in_app_price or 0),
                "title": product.title,
                "img_url": product.primary_image_url,
                "sku": product.sku,
            }
        ]
        subtotal = Decimal(str(product.in_app_price or 0))

    applied_coupon = None
    discount_amount = Decimal("0")
    if body.coupon_code:
        coupon_check = await check_coupon(
            db,
            code=body.coupon_code,
            merchant_id=product.merchant_id,
            order_amount=subtotal,
            lock=True,
        )
        if not coupon_check.valid:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=coupon_check.reason,
            )
        applied_coupon = coupon_check.coupon
        assert applied_coupon is not None
        discount_amount = coupon_check.discount_amount
        applied_coupon.used_count += 1

    # Never trust the client-supplied discount amount. The server calculates it
    # from the merchant-owned coupon record above.
    total = max(Decimal("0"), subtotal - discount_amount)

    lead = BuyerLead(
        merchant_id=product.merchant_id,
        user_id=user.id,
        lead_type=LeadType.DIRECT_PURCHASE.value,
        status=LeadStatus.NEW.value,
        product_ids=list(dict.fromkeys(i["product_id"] for i in items_payload)),
        ai_interactions_count=ai_count,
        estimated_value=total,
        # Fall back to user's saved profile if not provided at checkout time
        delivery_city=body.delivery_city or user.city,
        delivery_phone=body.delivery_phone or user.phone,
    )
    db.add(lead)
    await db.flush()

    order = Order(
        lead_id=lead.id,
        merchant_id=product.merchant_id,
        user_id=user.id,
        status=OrderStatus.PENDING_MERCHANT_CONTACT.value,
        items=items_payload,
        subtotal_estimated=subtotal,
        total_estimated=total,
        coupon_id=applied_coupon.id if applied_coupon else None,
        coupon_code=applied_coupon.code if applied_coupon else None,
        discount_amount=discount_amount,
        delivery_address={
            "city": lead.delivery_city,
            "phone": lead.delivery_phone,
            "address_line1": body.delivery_address_line1 or user.address_line1,
            "state": body.delivery_state or user.state,
            "pincode": body.delivery_pincode or user.pincode,
            "latitude": body.delivery_latitude,
            "longitude": body.delivery_longitude,
        },
        merchant_snapshot=OrderMerchantOut.model_validate(product.merchant).model_dump(mode="json"),
    )
    db.add(order)
    await db.commit()
    await db.refresh(lead)
    await db.refresh(order)

    # Notify merchant members about the new lead
    try:
        from app.models.merchant import MerchantMember
        from app.models.notification import Notification

        members_res = await db.execute(
            select(MerchantMember).where(MerchantMember.merchant_id == product.merchant_id)
        )
        merchant_members = members_res.scalars().all()

        for member in merchant_members:
            notif = Notification(
                user_id=member.user_id,
                kind="system",
                title="New Lead Received",
                summary=f"A new direct purchase lead has been received for product: {product.title}",
                payload={"lead_id": str(lead.id), "merchant_id": str(product.merchant_id)}
            )
            db.add(notif)
        await db.commit()
    except Exception as e:
        import logging
        logger = logging.getLogger("app.routers.buyer_leads")
        logger.warning(f"Failed to create merchant notifications: {e}")

    return BuyerLeadOut(
        merchant=OrderMerchantOut.model_validate(order.merchant_snapshot),
        id=lead.id,
        merchant_id=lead.merchant_id,
        lead_type=lead.lead_type,
        status=lead.status,
        estimated_value=lead.estimated_value,
        ai_interactions_count=lead.ai_interactions_count,
        ai_generated_image_url=None,
        delivery_city=lead.delivery_city,
        merchant_notes=None,
        converted_at=None,
        created_at=lead.created_at,
        updated_at=lead.updated_at,
        customer=CustomerInfo(
            city=lead.delivery_city,
            name=user.full_name,
            email=user.email,
            phone=lead.delivery_phone,
            address_line1=body.delivery_address_line1 or user.address_line1,
            state=body.delivery_state or user.state,
            pincode=body.delivery_pincode or user.pincode,
            latitude=body.delivery_latitude,
            longitude=body.delivery_longitude,
        ),
        order=OrderOut.model_validate(order),
    )


@router.get("/me", response_model=PaginatedLeads)
async def my_leads(
    user: CurrentUser,
    db: DBSession,
    limit: int = Query(default=25, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
):
    q = select(BuyerLead).where(BuyerLead.user_id == user.id)

    total_res = await db.execute(select(func.count()).select_from(q.subquery()))
    total = total_res.scalar_one()

    q = q.order_by(BuyerLead.created_at.desc()).limit(limit).offset(offset)
    res = await db.execute(q)
    leads = res.scalars().all()

    items = []
    for lead in leads:
        order_res = await db.execute(select(Order).where(Order.lead_id == lead.id))
        order_row = order_res.scalar_one_or_none()
        merchant = await db.get(Merchant, lead.merchant_id)
        snapshot = order_row.merchant_snapshot if order_row else None
        
        addr_dict = order_row.delivery_address if (order_row and order_row.delivery_address) else {}
        
        items.append(
            BuyerLeadOut(
                merchant=OrderMerchantOut.model_validate(snapshot or merchant) if (snapshot or merchant) else None,
                id=lead.id,
                merchant_id=lead.merchant_id,
                lead_type=lead.lead_type,
                status=lead.status,
                estimated_value=lead.estimated_value,
                ai_interactions_count=lead.ai_interactions_count,
                ai_generated_image_url=lead.ai_generated_image_url,
                delivery_city=lead.delivery_city,
                merchant_notes=lead.merchant_notes,
                cancellation_reason=lead.cancellation_reason,
                converted_at=lead.converted_at,
                created_at=lead.created_at,
                updated_at=lead.updated_at,
                customer=CustomerInfo(
                    city=addr_dict.get("city") or lead.delivery_city,
                    name=user.full_name,
                    email=user.email,
                    phone=addr_dict.get("phone") or lead.delivery_phone,
                    address_line1=addr_dict.get("address_line1") or user.address_line1,
                    state=addr_dict.get("state") or user.state,
                    pincode=addr_dict.get("pincode") or user.pincode,
                    latitude=addr_dict.get("latitude"),
                    longitude=addr_dict.get("longitude"),
                ),
                order=OrderOut.model_validate(order_row) if order_row else None,
            )
        )

    return PaginatedLeads(items=items, total=total, limit=limit, offset=offset)
