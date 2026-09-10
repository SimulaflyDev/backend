import uuid

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from app.models.merchant_product import MerchantProduct
from app.schemas.merchant_product import (
    MerchantProductCreate,
    MerchantProductOut,
    MerchantProductUpdate,
)
from app.utils.dependencies import DBSession
from app.utils.merchant_context import (
    CurrentMerchantContext,
    get_primary_merchant_id,
    require_verified_merchant,
)

router = APIRouter(
    prefix="/merchant/products",
    tags=["merchant-products"],
    dependencies=[Depends(require_verified_merchant)],
)


class PaginatedProducts(BaseModel):
    items: list[MerchantProductOut]
    total: int
    limit: int
    offset: int


@router.get("/", response_model=PaginatedProducts)
async def list_products(
    db: DBSession,
    ctx: CurrentMerchantContext,
    status_filter: str | None = Query(default=None, alias="status"),
    search: str | None = Query(default=None, min_length=1, max_length=200),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> dict:
    base = select(MerchantProduct).where(MerchantProduct.merchant_id == ctx.merchant.id)
    count_base = select(func.count()).select_from(MerchantProduct).where(
        MerchantProduct.merchant_id == ctx.merchant.id
    )

    if status_filter:
        base = base.where(MerchantProduct.status == status_filter)
        count_base = count_base.where(MerchantProduct.status == status_filter)
    if search:
        pat = f"%{search}%"
        base = base.where(
            or_(MerchantProduct.title.ilike(pat), MerchantProduct.sku.ilike(pat))
        )
        count_base = count_base.where(
            or_(MerchantProduct.title.ilike(pat), MerchantProduct.sku.ilike(pat))
        )

    total_res = await db.execute(count_base)
    total = total_res.scalar_one()

    base = (
        base.options(selectinload(MerchantProduct.variants))
        .order_by(MerchantProduct.created_at.desc())
        .offset(offset)
        .limit(limit)
    )
    rows = (await db.execute(base)).scalars().all()
    return {"items": list(rows), "total": total, "limit": limit, "offset": offset}


@router.post("/", response_model=MerchantProductOut, status_code=status.HTTP_201_CREATED)
async def create_product(
    body: MerchantProductCreate,
    db: DBSession,
    ctx: CurrentMerchantContext,
    background_tasks: BackgroundTasks,
) -> MerchantProduct:
    if not ctx.merchant.settings.get("onboarding_completed", False):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Merchant onboarding must be completed before adding products.",
        )

    # Resolve all shop IDs user belongs to
    from app.models.merchant import MerchantMember
    user_memberships_res = await db.execute(
        select(MerchantMember.merchant_id).where(MerchantMember.user_id == ctx.member.user_id)
    )
    user_shop_ids = set(user_memberships_res.scalars().all())

    target_shop_ids = body.shop_ids if body.shop_ids is not None else [ctx.merchant.id]
    if not target_shop_ids:
        target_shop_ids = [ctx.merchant.id]

    # Ensure they are member of all target shops
    for sid in target_shop_ids:
        if sid not in user_shop_ids:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"You do not have access to shop ID {sid}"
            )

    from app.models.merchant import Merchant
    verified_shops_res = await db.execute(
        select(Merchant.id).where(
            Merchant.id.in_(target_shop_ids),
            Merchant.is_kyc_completed.is_(True),
        )
    )
    verified_shop_ids = set(verified_shops_res.scalars().all())
    unverified_shop_ids = [str(sid) for sid in target_shop_ids if sid not in verified_shop_ids]
    if unverified_shop_ids:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Shop verification is required for shop IDs: {', '.join(unverified_shop_ids)}",
        )

    payload = body.model_dump()
    payload.pop("shop_ids", None)

    main_product = None
    from app.services.embedding import regenerate_embedding

    for sid in target_shop_ids:
        product = MerchantProduct(
            merchant_id=sid,
            **payload,
        )
        db.add(product)
        try:
            await db.flush()
        except IntegrityError:
            await db.rollback()
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"A product with SKU '{body.sku}' already exists in one of the selected shops."
            )
        
        # Schedule embedding regen (non-blocking)
        background_tasks.add_task(regenerate_embedding, db, product.id)

        if sid == ctx.merchant.id:
            main_product = product

    await db.commit()

    if not main_product:
        main_product = product

    # Reload eagerly to avoid lazy-load greenlet errors
    stmt = (
        select(MerchantProduct)
        .options(selectinload(MerchantProduct.variants))
        .where(MerchantProduct.id == main_product.id)
    )
    main_product = (await db.execute(stmt)).scalar_one()
    return main_product


@router.get("/{product_id}", response_model=MerchantProductOut)
async def get_product(
    product_id: uuid.UUID, db: DBSession, ctx: CurrentMerchantContext
) -> MerchantProduct:
    stmt = (
        select(MerchantProduct)
        .options(selectinload(MerchantProduct.variants))
        .where(
            MerchantProduct.id == product_id,
            MerchantProduct.merchant_id == ctx.merchant.id,
        )
    )
    product = (await db.execute(stmt)).scalar_one_or_none()
    if not product:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="product not found")
    return product


def _embedding_fields_changed(body: MerchantProductUpdate, current: MerchantProduct) -> bool:
    data = body.model_dump(exclude_unset=True)
    for key in ("title", "description", "category"):
        if key in data and data[key] != getattr(current, key):
            return True
    return False


@router.patch("/{product_id}", response_model=MerchantProductOut)
async def update_product(
    product_id: uuid.UUID,
    body: MerchantProductUpdate,
    db: DBSession,
    ctx: CurrentMerchantContext,
    background_tasks: BackgroundTasks,
) -> MerchantProduct:
    stmt = (
        select(MerchantProduct)
        .options(selectinload(MerchantProduct.variants))
        .where(
            MerchantProduct.id == product_id,
            MerchantProduct.merchant_id == ctx.merchant.id,
        )
    )
    product = (await db.execute(stmt)).scalar_one_or_none()
    if not product:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="product not found")

    needs_embedding_regen = _embedding_fields_changed(body, product)

    data = body.model_dump(exclude_unset=True)
    effective_primary = data.get("primary_image_url", product.primary_image_url)
    effective_additional = data.get("additional_images", product.additional_images) or []
    if effective_additional and not effective_primary:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="primary_image_url is required when additional images are provided",
        )
    if effective_primary and effective_primary in effective_additional:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="product image URLs must be unique",
        )
    for k, v in data.items():
        setattr(product, k, v)

    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="conflict (duplicate SKU?)")

    # Re-fetch with eager-loaded relations to avoid MissingGreenlet on serialization.
    stmt = (
        select(MerchantProduct)
        .options(selectinload(MerchantProduct.variants))
        .where(MerchantProduct.id == product_id)
    )
    product = (await db.execute(stmt)).scalar_one()

    if needs_embedding_regen:
        from app.services.embedding import regenerate_embedding
        background_tasks.add_task(regenerate_embedding, db, product.id)

    return product


@router.delete("/{product_id}", status_code=status.HTTP_204_NO_CONTENT)
async def archive_product(
    product_id: uuid.UUID, db: DBSession, ctx: CurrentMerchantContext
) -> None:
    product = await db.get(MerchantProduct, product_id)
    if not product or product.merchant_id != ctx.merchant.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="product not found")
    product.status = "archived"
    product.has_simulafly_listing = False
    await db.commit()


@router.post("/{product_id}/publish", response_model=MerchantProductOut)
async def publish_product(
    product_id: uuid.UUID, db: DBSession, ctx: CurrentMerchantContext
) -> MerchantProduct:
    stmt = (
        select(MerchantProduct)
        .options(selectinload(MerchantProduct.variants))
        .where(
            MerchantProduct.id == product_id,
            MerchantProduct.merchant_id == ctx.merchant.id,
        )
    )
    product = (await db.execute(stmt)).scalar_one_or_none()
    if not product:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="product not found")

    # Phase 3: enforce wallet balance >= threshold before publishing
    from app.models.wallet import Wallet
    res = await db.execute(select(Wallet).where(Wallet.merchant_id == ctx.merchant.id))
    wallet = res.scalar_one_or_none()
    if not wallet or wallet.balance < wallet.low_balance_threshold:
        raise HTTPException(
            status_code=402,
            detail="wallet balance below threshold; top up before publishing",
        )

    product.status = "published"
    product.has_simulafly_listing = True
    await db.commit()

    # Re-fetch for serialization
    stmt = (
        select(MerchantProduct)
        .options(selectinload(MerchantProduct.variants))
        .where(MerchantProduct.id == product_id)
    )
    product = (await db.execute(stmt)).scalar_one()
    return product


from app.models.merchant_product import MerchantProductVariant
from app.schemas.merchant_product import (
    ProductVariantCreate,
    ProductVariantOut,
    ProductVariantUpdate,
)


async def _product_owned(
    db: DBSession, product_id: uuid.UUID, merchant_id: uuid.UUID
) -> MerchantProduct:
    """Fetch a product or raise 404 if missing / owned by another merchant."""
    p = await db.get(MerchantProduct, product_id)
    if not p or p.merchant_id != merchant_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="product not found")
    return p


# ─────────────────────────── Variants ────────────────────────────────────────


@router.get("/{product_id}/variants/", response_model=list[ProductVariantOut])
async def list_variants(
    product_id: uuid.UUID,
    db: DBSession,
    ctx: CurrentMerchantContext,
) -> list[MerchantProductVariant]:
    await _product_owned(db, product_id, ctx.merchant.id)
    res = await db.execute(
        select(MerchantProductVariant)
        .where(MerchantProductVariant.merchant_product_id == product_id)
        .order_by(MerchantProductVariant.position)
    )
    return list(res.scalars().all())


@router.post(
    "/{product_id}/variants/",
    response_model=ProductVariantOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_variant(
    product_id: uuid.UUID,
    body: ProductVariantCreate,
    db: DBSession,
    ctx: CurrentMerchantContext,
) -> MerchantProductVariant:
    await _product_owned(db, product_id, ctx.merchant.id)
    variant = MerchantProductVariant(
        merchant_product_id=product_id,
        **body.model_dump(),
    )
    db.add(variant)
    await db.commit()
    await db.refresh(variant)
    return variant


@router.patch("/{product_id}/variants/{variant_id}", response_model=ProductVariantOut)
async def update_variant(
    product_id: uuid.UUID,
    variant_id: uuid.UUID,
    body: ProductVariantUpdate,
    db: DBSession,
    ctx: CurrentMerchantContext,
) -> MerchantProductVariant:
    await _product_owned(db, product_id, ctx.merchant.id)
    variant = await db.get(MerchantProductVariant, variant_id)
    if not variant or variant.merchant_product_id != product_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="variant not found")
    for k, v in body.model_dump(exclude_unset=True).items():
        setattr(variant, k, v)
    await db.commit()
    await db.refresh(variant)
    return variant


@router.delete("/{product_id}/variants/{variant_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_variant(
    product_id: uuid.UUID,
    variant_id: uuid.UUID,
    db: DBSession,
    ctx: CurrentMerchantContext,
) -> None:
    await _product_owned(db, product_id, ctx.merchant.id)
    variant = await db.get(MerchantProductVariant, variant_id)
    if not variant or variant.merchant_product_id != product_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="variant not found")
    await db.delete(variant)
    await db.commit()
