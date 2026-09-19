"""Buyer Intelligence endpoints for the merchant portal.

High-intent ShopperFeed:
  GET  /merchant/buyer-intelligence/       — paginated list of shoppers who
       interacted with this merchant's products, with computed intent score.
       Non-unlocked shoppers only show city.
  POST /merchant/buyer-intelligence/{user_id}/unlock — deduct the configured fee, reveal contact.
  GET  /merchant/buyer-intelligence/unlocked          — already-unlocked contacts.

Intent score formula (per-user, per-merchant):
  click              ×1
  ai_rag_mention     ×2
  ai_image_generation ×5
  external_redirect  ×8
  simulafly_purchase ×15  (from lead conversions)
Score capped at 99.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from app.models.buyer_intelligence import MerchantBuyerAccess
from app.models.event import BuyerEvent
from app.models.lead import BuyerLead
from app.models.user import User
from app.models.wallet import Wallet
from app.services.pricing import resolve_rate
from app.utils.dependencies import DBSession
from app.utils.merchant_context import (
    CurrentMerchantContext,
    get_primary_merchant_id,
    require_verified_merchant,
)

router = APIRouter(
    prefix="/merchant/buyer-intelligence",
    tags=["buyer-intelligence"],
    dependencies=[Depends(require_verified_merchant)],
)

DEFAULT_UNLOCK_COST = Decimal("50.00")


async def _resolve_unlock_cost(db: DBSession, merchant_id: uuid.UUID) -> Decimal:
    """Return the admin-configured flat buyer-intelligence unlock price."""
    rate, rate_type = await resolve_rate(db, "lead_unlocked", merchant_id)
    if rate_type != "fixed":
        return DEFAULT_UNLOCK_COST
    return rate


def _uuid_param(db: DBSession, value: uuid.UUID) -> uuid.UUID | str:
    """Bind UUIDs correctly for both PostgreSQL and the SQLite test database."""
    return value.hex if db.get_bind().dialect.name == "sqlite" else value


def _as_uuid(value: uuid.UUID | str) -> uuid.UUID:
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))

# Intent weights
_WEIGHTS: dict[str, int] = {
    "click": 1,
    "ai_rag_mention": 2,
    "ai_image_generation": 5,
    "simulafly_purchase": 15,
}


def _intent_score(events: list[tuple[str, int]]) -> int:
    raw = sum(_WEIGHTS.get(ev_type, 0) * count for ev_type, count in events)
    return min(raw, 99)


def _intent_label(score: int) -> tuple[str, str]:
    """Returns (label, tier) based on score."""
    if score >= 80:
        return "Purchase Ready", "ready"
    if score >= 55:
        return "High Intent", "high"
    if score >= 30:
        return "Medium Intent", "medium"
    return "Low Intent", "low"


class ShopperOut(BaseModel):
    user_id: str
    city: str
    # only populated when unlocked
    name: str | None = None
    phone: str | None = None
    email: str | None = None
    intent_score: int
    intent_label: str
    intent_tier: str
    interaction_count: int
    unlocked: bool
    unlock_cost: float
    # per-event-type counts
    click_count: int = 0
    rag_count: int = 0
    image_count: int = 0
    redirect_count: int = 0


class PaginatedShoppers(BaseModel):
    items: list[ShopperOut]
    total: int
    limit: int
    offset: int
    unlock_cost: float


class UnlockResponse(BaseModel):
    user_id: str
    name: str
    phone: str | None
    email: str | None
    city: str
    intent_score: int
    intent_label: str
    intent_tier: str
    interaction_count: int
    unlocked: bool = True
    unlock_cost: float
    click_count: int
    rag_count: int
    image_count: int
    redirect_count: int


async def _aggregate_shoppers(
    db: DBSession,
    merchant_id: uuid.UUID,
    since_days: int = 30,
) -> list[dict]:
    """Aggregate buyer events per user for this merchant."""
    since = datetime.now(timezone.utc) - timedelta(days=since_days)
    sql = text(
        """
        SELECT
            be.user_id,
            COUNT(*) FILTER (WHERE be.event_type = 'click') AS click_count,
            COUNT(*) FILTER (WHERE be.event_type = 'ai_rag_mention') AS rag_count,
            COUNT(*) FILTER (WHERE be.event_type = 'ai_image_generation') AS image_count,
            COUNT(*) FILTER (WHERE be.event_type = 'external_redirect') AS redirect_count,
            COUNT(*) AS total_interactions
        FROM buyer_events be
        WHERE be.merchant_id = :mid
          AND be.created_at >= :since
        GROUP BY be.user_id
        HAVING COUNT(*) >= 1
        ORDER BY (
            COUNT(*) FILTER (WHERE be.event_type = 'click') * 1 +
            COUNT(*) FILTER (WHERE be.event_type = 'ai_rag_mention') * 2 +
            COUNT(*) FILTER (WHERE be.event_type = 'ai_image_generation') * 5
        ) DESC
        """
    )
    res = await db.execute(sql, {"mid": _uuid_param(db, merchant_id), "since": since})
    return [dict(r._mapping) for r in res.fetchall()]


@router.get("/", response_model=PaginatedShoppers)
async def list_shoppers(
    db: DBSession,
    ctx: CurrentMerchantContext,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    since_days: int = Query(default=30, ge=1, le=90),
) -> dict:
    rows = await _aggregate_shoppers(db, ctx.merchant.id, since_days)
    unlock_cost = await _resolve_unlock_cost(db, ctx.merchant.id)
    total = len(rows)
    page = rows[offset: offset + limit]

    if not page:
        return {
            "items": [],
            "total": total,
            "limit": limit,
            "offset": offset,
            "unlock_cost": float(unlock_cost),
        }

    user_ids = [_as_uuid(r["user_id"]) for r in page]

    # Fetch user profiles
    users_res = await db.execute(select(User).where(User.id.in_(user_ids)))
    users_by_id = {u.id: u for u in users_res.scalars().all()}

    # Fetch which ones are already unlocked
    unlocked_res = await db.execute(
        select(MerchantBuyerAccess.user_id).where(
            MerchantBuyerAccess.merchant_id == ctx.merchant.id,
            MerchantBuyerAccess.user_id.in_(user_ids),
        )
    )
    unlocked_ids = {row[0] for row in unlocked_res.fetchall()}

    # Fetch latest lead per user (for city + phone)
    leads_res = await db.execute(
        select(BuyerLead)
        .where(
            BuyerLead.merchant_id == ctx.merchant.id,
            BuyerLead.user_id.in_(user_ids),
        )
        .order_by(BuyerLead.created_at.desc())
    )
    leads_by_user: dict[uuid.UUID, BuyerLead] = {}
    for lead in leads_res.scalars().all():
        leads_by_user.setdefault(lead.user_id, lead)

    items: list[ShopperOut] = []
    for r in page:
        uid = _as_uuid(r["user_id"])
        user = users_by_id.get(uid)
        if not user:
            continue
        lead = leads_by_user.get(uid)
        events = [
            ("click", r["click_count"]),
            ("ai_rag_mention", r["rag_count"]),
            ("ai_image_generation", r["image_count"]),
        ]
        score = _intent_score(events)
        label, tier = _intent_label(score)
        is_unlocked = uid in unlocked_ids
        items.append(
            ShopperOut(
                user_id=str(uid),
                city=lead.delivery_city if lead else "India",
                name=user.full_name if is_unlocked else None,
                phone=lead.delivery_phone if (is_unlocked and lead) else None,
                email=user.email if is_unlocked else None,
                intent_score=score,
                intent_label=label,
                intent_tier=tier,
                interaction_count=int(r["total_interactions"]),
                unlocked=is_unlocked,
                unlock_cost=float(unlock_cost),
                click_count=int(r["click_count"]),
                rag_count=int(r["rag_count"]),
                image_count=int(r["image_count"]),
                redirect_count=int(r["redirect_count"]),
            )
        )

    return {
        "items": items,
        "total": total,
        "limit": limit,
        "offset": offset,
        "unlock_cost": float(unlock_cost),
    }


@router.post("/{user_id}/unlock", response_model=UnlockResponse)
async def unlock_shopper(
    user_id: uuid.UUID,
    db: DBSession,
    ctx: CurrentMerchantContext,
) -> UnlockResponse:
    # Check if already unlocked
    existing = await db.execute(
        select(MerchantBuyerAccess).where(
            MerchantBuyerAccess.merchant_id == ctx.merchant.id,
            MerchantBuyerAccess.user_id == user_id,
        )
    )
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="already unlocked")

    # Calculate intent score and load the merchant's admin-configured unlock fee.
    since = datetime.now(timezone.utc) - timedelta(days=30)
    sql = text(
        """
        SELECT
            COUNT(*) FILTER (WHERE event_type = 'click') AS click_count,
            COUNT(*) FILTER (WHERE event_type = 'ai_rag_mention') AS rag_count,
            COUNT(*) FILTER (WHERE event_type = 'ai_image_generation') AS image_count,
            COUNT(*) FILTER (WHERE event_type = 'external_redirect') AS redirect_count
        FROM buyer_events
        WHERE merchant_id = :mid AND user_id = :uid AND created_at >= :since
        """
    )
    ev_res = await db.execute(
        sql,
        {
            "mid": _uuid_param(db, ctx.merchant.id),
            "uid": _uuid_param(db, user_id),
            "since": since,
        },
    )
    ev_row = ev_res.fetchone()
    
    click_count = 0
    rag_count = 0
    image_count = 0
    if ev_row:
        ev = dict(ev_row._mapping)
        click_count = ev.get("click_count") or 0
        rag_count = ev.get("rag_count") or 0
        image_count = ev.get("image_count") or 0

    events = [
        ("click", click_count),
        ("ai_rag_mention", rag_count),
        ("ai_image_generation", image_count),
    ]
    score = _intent_score(events)
    unlock_cost = await _resolve_unlock_cost(db, ctx.merchant.id)

    # Check wallet balance
    wallet_res = await db.execute(
        select(Wallet).where(Wallet.merchant_id == ctx.merchant.id)
    )
    wallet = wallet_res.scalar_one_or_none()
    if not wallet or wallet.balance < unlock_cost:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail=f"insufficient wallet balance; need ₹{unlock_cost}",
        )

    # Deduct from wallet
    wallet.balance = wallet.balance - unlock_cost

    # Record unlock (we keep ctx.merchant.id for tracing unlock source)
    access = MerchantBuyerAccess(
        merchant_id=ctx.merchant.id,
        user_id=user_id,
        unlock_cost=unlock_cost,
    )
    db.add(access)

    # Add ledger entry for unlock deduction
    from app.models.event import LedgerEntry
    ledger = LedgerEntry(
        merchant_id=ctx.merchant.id,
        wallet_id=wallet.id,
        entry_type="deduction",
        amount=-unlock_cost,
        reason="buyer_intel_unlock",
        balance_after=wallet.balance,
        notes=f"Unlocked buyer profile ({user_id}) with intent score {score}"
    )
    db.add(ledger)

    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="already unlocked")

    # Fetch user
    user = await db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="user not found")

    # Credit user ₹20 on merchant unlock
    from app.services.user_tokens import credit_tokens
    await credit_tokens(db, user, 20.0)
    await db.commit()

    # Fetch latest lead for city + phone
    lead_res = await db.execute(
        select(BuyerLead)
        .where(BuyerLead.merchant_id == ctx.merchant.id, BuyerLead.user_id == user_id)
        .order_by(BuyerLead.created_at.desc())
        .limit(1)
    )
    lead = lead_res.scalar_one_or_none()

    # Re-aggregate events for this user
    since = datetime.now(timezone.utc) - timedelta(days=30)
    sql = text(
        """
        SELECT
            COUNT(*) FILTER (WHERE event_type = 'click') AS click_count,
            COUNT(*) FILTER (WHERE event_type = 'ai_rag_mention') AS rag_count,
            COUNT(*) FILTER (WHERE event_type = 'ai_image_generation') AS image_count,
            COUNT(*) FILTER (WHERE event_type = 'external_redirect') AS redirect_count,
            COUNT(*) AS total_interactions
        FROM buyer_events
        WHERE merchant_id = :mid AND user_id = :uid AND created_at >= :since
        """
    )
    ev_res = await db.execute(
        sql,
        {
            "mid": _uuid_param(db, ctx.merchant.id),
            "uid": _uuid_param(db, user_id),
            "since": since,
        },
    )
    ev = dict(ev_res.fetchone()._mapping)
    events = [
        ("click", ev["click_count"]),
        ("ai_rag_mention", ev["rag_count"]),
        ("ai_image_generation", ev["image_count"]),
    ]
    score = _intent_score(events)
    label, tier = _intent_label(score)

    return UnlockResponse(
        user_id=str(user_id),
        name=user.full_name or user.email,
        phone=lead.delivery_phone if lead else None,
        email=user.email,
        city=lead.delivery_city if lead else "India",
        intent_score=score,
        intent_label=label,
        intent_tier=tier,
        interaction_count=int(ev["total_interactions"]),
        unlock_cost=float(unlock_cost),
        click_count=int(ev["click_count"]),
        rag_count=int(ev["rag_count"]),
        image_count=int(ev["image_count"]),
        redirect_count=int(ev["redirect_count"]),
    )


class ShopperProductInteraction(BaseModel):
    name: str
    views: int
    engagement: str


class ShopperTimelineEvent(BaseModel):
    time: str
    icon: str
    text: str
    type: str


class ShopperDetailResponse(BaseModel):
    user_id: str
    city: str
    name: str | None = None
    phone: str | None = None
    email: str | None = None
    intent_score: int
    intent_label: str
    intent_tier: str
    unlocked: bool
    unlock_cost: float
    interaction_count: int
    click_count: int = 0
    rag_count: int = 0
    image_count: int = 0
    redirect_count: int = 0
    total_orders: int = 0
    lifetime_spend: float = 0.0
    viewed_products: list[ShopperProductInteraction] = []
    timeline: list[ShopperTimelineEvent] = []
    intent_reasons: list[str] = []
    suggested_bundle: list[str] = []


@router.get("/{user_id}", response_model=ShopperDetailResponse)
async def shopper_detail(
    user_id: uuid.UUID,
    db: DBSession,
    ctx: CurrentMerchantContext,
) -> dict:
    mid = ctx.merchant.id
    unlock_cost = await _resolve_unlock_cost(db, mid)

    # Fetch user
    user = await db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    # Fetch which ones are already unlocked
    unlocked_res = await db.execute(
        select(MerchantBuyerAccess).where(
            MerchantBuyerAccess.merchant_id == mid,
            MerchantBuyerAccess.user_id == user_id,
        )
    )
    is_unlocked = unlocked_res.scalar_one_or_none() is not None

    # Fetch latest lead for city + phone
    lead_res = await db.execute(
        select(BuyerLead)
        .where(BuyerLead.merchant_id == mid, BuyerLead.user_id == user_id)
        .order_by(BuyerLead.created_at.desc())
        .limit(1)
    )
    lead = lead_res.scalar_one_or_none()

    # Re-aggregate events for this user
    since = datetime.now(timezone.utc) - timedelta(days=30)
    sql = text(
        """
        SELECT
            COUNT(*) FILTER (WHERE event_type = 'click') AS click_count,
            COUNT(*) FILTER (WHERE event_type = 'ai_rag_mention') AS rag_count,
            COUNT(*) FILTER (WHERE event_type = 'ai_image_generation') AS image_count,
            COUNT(*) FILTER (WHERE event_type = 'external_redirect') AS redirect_count,
            COUNT(*) AS total_interactions
        FROM buyer_events
        WHERE merchant_id = :mid AND user_id = :uid AND created_at >= :since
        """
    )
    ev_res = await db.execute(
        sql,
        {
            "mid": _uuid_param(db, mid),
            "uid": _uuid_param(db, user_id),
            "since": since,
        },
    )
    ev = dict(ev_res.fetchone()._mapping)
    events = [
        ("click", ev["click_count"]),
        ("ai_rag_mention", ev["rag_count"]),
        ("ai_image_generation", ev["image_count"]),
    ]
    score = _intent_score(events)
    label, tier = _intent_label(score)

    # Count converted leads and calculate spend
    orders_stmt = select(func.count(BuyerLead.id), func.coalesce(func.sum(BuyerLead.estimated_value), 0)).where(
        BuyerLead.merchant_id == mid,
        BuyerLead.user_id == user_id,
        BuyerLead.status == "converted",
    )
    orders_res = (await db.execute(orders_stmt)).fetchone()
    total_orders = int(orders_res[0] or 0)
    lifetime_spend = float(orders_res[1] or 0)

    # Viewed products
    from app.models.merchant_product import MerchantProduct
    prod_stmt = (
        select(
            BuyerEvent.merchant_product_id,
            MerchantProduct.title,
            func.count(BuyerEvent.id).label("views")
        )
        .join(MerchantProduct, MerchantProduct.id == BuyerEvent.merchant_product_id)
        .where(
            BuyerEvent.merchant_id == mid,
            BuyerEvent.user_id == user_id,
            BuyerEvent.merchant_product_id.is_not(None)
        )
        .group_by(BuyerEvent.merchant_product_id, MerchantProduct.title)
        .order_by(func.count(BuyerEvent.id).desc())
    )
    prod_rows = (await db.execute(prod_stmt)).all()
    viewed_products = [
        {
            "name": r.title,
            "views": r.views,
            "engagement": f"{r.views} interaction{'s' if r.views != 1 else ''} recorded"
        }
        for r in prod_rows
    ]

    # Timeline events
    timeline_stmt = (
        select(BuyerEvent)
        .where(
            BuyerEvent.merchant_id == mid,
            BuyerEvent.user_id == user_id
        )
        .order_by(BuyerEvent.created_at.desc())
        .limit(15)
    )
    timeline_events = (await db.execute(timeline_stmt)).scalars().all()
    
    pids = {e.merchant_product_id for e in timeline_events if e.merchant_product_id}
    product_titles = {}
    if pids:
        p_stmt = select(MerchantProduct.id, MerchantProduct.title).where(MerchantProduct.id.in_(list(pids)))
        p_rows = (await db.execute(p_stmt)).all()
        product_titles = {row.id: row.title for row in p_rows}

    timeline = []
    for event in timeline_events:
        t = "view"
        icon = "👁️"
        text_label = "Interacted with product"
        if event.event_type == "click":
            t = "view"
            icon = "👁️"
            text_label = "Clicked product details"
        elif event.event_type == "ai_rag_mention":
            t = "view"
            icon = "💬"
            text_label = "Surfaced in AI search"
        elif event.event_type == "ai_image_generation":
            t = "room"
            icon = "🛋️"
            text_label = "Generated a room preview"

        
        title = product_titles.get(event.merchant_product_id)
        if title:
            text_label = f"{text_label} for {title}"
        
        timeline.append({
            "time": event.created_at.strftime("%Y-%m-%d %H:%M"),
            "icon": icon,
            "text": text_label,
            "type": t
        })

    # Intent reasons
    intent_reasons = []
    if ev["image_count"] > 0:
        intent_reasons.append(f"Generated {ev['image_count']} room preview{'s' if ev['image_count'] != 1 else ''}")
    if ev["click_count"] > 0:
        intent_reasons.append(f"Clicked product details {ev['click_count']} time{'s' if ev['click_count'] != 1 else ''}")
    if ev["redirect_count"] > 0:
        intent_reasons.append(f"Initiated store redirect {ev['redirect_count']} time{'s' if ev['redirect_count'] != 1 else ''}")
    if ev["rag_count"] > 0:
        intent_reasons.append(f"Surfaced in AI conversations {ev['rag_count']} time{'s' if ev['rag_count'] != 1 else ''}")

    # Suggested bundle
    suggested_bundle = [p["name"] for p in viewed_products[:3]]

    return {
        "user_id": str(user_id),
        "city": lead.delivery_city if lead else "India",
        "name": user.full_name if is_unlocked else None,
        "phone": lead.delivery_phone if (is_unlocked and lead) else None,
        "email": user.email if is_unlocked else None,
        "intent_score": score,
        "intent_label": label,
        "intent_tier": tier,
        "unlocked": is_unlocked,
        "unlock_cost": float(unlock_cost),
        "interaction_count": int(ev["total_interactions"]),
        "click_count": int(ev["click_count"]),
        "rag_count": int(ev["rag_count"]),
        "image_count": int(ev["image_count"]),
        "redirect_count": int(ev["redirect_count"]),
        "total_orders": total_orders,
        "lifetime_spend": lifetime_spend,
        "viewed_products": viewed_products,
        "timeline": timeline,
        "intent_reasons": intent_reasons,
        "suggested_bundle": suggested_bundle,
    }

