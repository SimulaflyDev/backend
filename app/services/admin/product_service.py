"""Admin product directory service (R10 — product directory).

The :class:`ProductService` backs the admin ``products`` router. It implements:

  * **List** products (paginated) — each item exposes ``id``, ``title``, the
    owning merchant (``merchant_id`` and resolved ``merchant_name``), ``price``
    (in-app price), ``status``, and ``created_at`` (R10.1). Listings support a
    title search (R10.2), conjunctive merchant/status/price-range filters
    (R10.3), and whitelisted sorting — all delegated to the shared listing
    engine (:func:`app.services.admin.listing.paginate`).
  * **Detail** — fetch a single product by id, raising HTTP 404 when missing
    (R10.4, R10.6).
  * **Update visibility status** — persist a product's new status (R10.5); the
    router wraps this in an ``audited(...)`` dependency so the action is
    recorded.

Admin "delete" is deliberately implemented as an archive transition: the row
is retained for audit/recovery while ``has_simulafly_listing`` is disabled, so
it disappears from customer-facing product queries.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal

from fastapi import HTTPException, status as http_status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.merchant import Merchant
from app.models.merchant_product import MerchantProduct, ProductStatus
from app.schemas.admin.products import ProductCreateRequest, ProductUpdateRequest
from app.services.admin.listing import ListParams, Page, paginate

#: Whitelist of sortable fields → column expressions for the product listing.
_SORTABLE = {
    "title": MerchantProduct.title,
    "price": MerchantProduct.in_app_price,
    "status": MerchantProduct.status,
    "created_at": MerchantProduct.created_at,
    "merchant_id": MerchantProduct.merchant_id,
}

#: Columns OR-matched (ILIKE) against the search term — title only (R10.2).
_SEARCHABLE = (MerchantProduct.title,)


@dataclass
class ProductListParams:
    """Inputs to :meth:`ProductService.list_products`.

    ``page`` is 1-based; ``page_size`` is clamped by the listing engine. Filters
    are conjunctive (R10.3): ``merchant_id`` / ``status`` are equality matches,
    and ``min_price`` / ``max_price`` bound the in-app price range (inclusive).
    """

    page: int = 1
    page_size: int | None = None
    search: str | None = None
    sort: str | None = None
    merchant_id: uuid.UUID | None = None
    status: str | None = None
    min_price: Decimal | None = None
    max_price: Decimal | None = None


@dataclass
class ProductListRow:
    """A flattened product listing row including the resolved merchant name."""

    id: uuid.UUID
    sku: str
    title: str
    merchant_id: uuid.UUID
    merchant_name: str | None
    price: Decimal | None
    category: str | None
    primary_image_url: str | None
    in_app_stock: int | None
    status: str
    created_at: object


class ProductService:
    """Read/manage operations for the product directory (R10)."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def list_products(self, params: ProductListParams) -> Page:
        """Return a paginated page of products (R10.1–R10.3).

        Applies the title search (R10.2) and conjunctive merchant/status/
        price-range filters (R10.3) before delegating pagination, sorting, and
        the envelope metadata to the shared listing engine. The returned
        :class:`Page` carries :class:`ProductListRow` items with the owning
        merchant's display name resolved in a single batched lookup.
        """
        base_stmt = select(MerchantProduct)

        # Price-range filter (R10.3) — applied to the base statement since the
        # shared engine handles equality/membership filters only.
        if params.min_price is not None:
            base_stmt = base_stmt.where(MerchantProduct.in_app_price >= params.min_price)
        if params.max_price is not None:
            base_stmt = base_stmt.where(MerchantProduct.in_app_price <= params.max_price)

        list_params = ListParams(
            page=params.page,
            page_size=params.page_size if params.page_size is not None else ListParams().page_size,
            search=params.search,
            sort=params.sort,
            filters={
                MerchantProduct.merchant_id: params.merchant_id,
                MerchantProduct.status: params.status,
            },
        )

        page = await paginate(
            self.db,
            base_stmt,
            params=list_params,
            sortable=_SORTABLE,
            searchable=_SEARCHABLE,
        )

        merchant_names = await self._resolve_merchant_names(
            {product.merchant_id for product in page.items}
        )

        page.items = [
            ProductListRow(
                id=product.id,
                sku=product.sku,
                title=product.title,
                merchant_id=product.merchant_id,
                merchant_name=merchant_names.get(product.merchant_id),
                price=product.in_app_price,
                category=product.category,
                primary_image_url=product.primary_image_url,
                in_app_stock=product.in_app_stock,
                status=product.status,
                created_at=product.created_at,
            )
            for product in page.items
        ]
        return page

    async def get_product(self, product_id: uuid.UUID) -> MerchantProduct:
        """Return a single product by id, or raise HTTP 404 (R10.4, R10.6)."""
        product = await self.db.get(MerchantProduct, product_id)
        if product is None:
            raise HTTPException(
                status_code=http_status.HTTP_404_NOT_FOUND,
                detail="product not found",
            )
        return product

    async def get_merchant_name(self, merchant_id: uuid.UUID) -> str | None:
        """Resolve the owning merchant's display name (for the detail view)."""
        names = await self._resolve_merchant_names({merchant_id})
        return names.get(merchant_id)

    async def create_product(self, payload: ProductCreateRequest) -> MerchantProduct:
        """Create one product for an existing merchant shop."""
        await self._ensure_merchants_exist({payload.merchant_id})
        product = self._build_product(payload)
        self.db.add(product)
        try:
            await self.db.commit()
        except IntegrityError as exc:
            await self.db.rollback()
            raise HTTPException(
                status_code=http_status.HTTP_409_CONFLICT,
                detail=f"SKU '{payload.sku}' already exists for this merchant",
            ) from exc
        await self.db.refresh(product)
        return product

    async def bulk_create_products(
        self, payloads: list[ProductCreateRequest]
    ) -> list[MerchantProduct]:
        """Create a bounded batch atomically; any conflict rolls back all rows."""
        await self._ensure_merchants_exist({item.merchant_id for item in payloads})
        products = [self._build_product(item) for item in payloads]
        self.db.add_all(products)
        try:
            await self.db.commit()
        except IntegrityError as exc:
            await self.db.rollback()
            raise HTTPException(
                status_code=http_status.HTTP_409_CONFLICT,
                detail="Bulk add failed because one or more SKUs already exist",
            ) from exc
        for product in products:
            await self.db.refresh(product)
        return products

    async def update_product(
        self, product_id: uuid.UUID, payload: ProductUpdateRequest
    ) -> MerchantProduct:
        """Update editable product fields while preserving its database row."""
        product = await self.get_product(product_id)
        data = payload.model_dump(exclude_unset=True)
        if "price" in data:
            data["in_app_price"] = data.pop("price")
        status_value = data.get("status")
        if status_value is not None:
            data["status"] = status_value.value
            data["has_simulafly_listing"] = status_value == ProductStatus.PUBLISHED
        for key, value in data.items():
            setattr(product, key, value)
        try:
            await self.db.commit()
        except IntegrityError as exc:
            await self.db.rollback()
            raise HTTPException(
                status_code=http_status.HTTP_409_CONFLICT,
                detail="A product with this SKU already exists for the merchant",
            ) from exc
        await self.db.refresh(product)
        return product

    async def archive_product(self, product_id: uuid.UUID) -> MerchantProduct:
        """Soft-delete a product by archiving it and removing app visibility."""
        return await self.update_status(product_id, ProductStatus.ARCHIVED)

    async def update_status(
        self,
        product_id: uuid.UUID,
        new_status: ProductStatus,
    ) -> MerchantProduct:
        """Persist a product's new visibility status (R10.5).

        Raises HTTP 404 when the product does not exist (R10.6). The router
        records the action via its ``audited(...)`` dependency.
        """
        product = await self.get_product(product_id)
        product.status = new_status.value
        product.has_simulafly_listing = new_status == ProductStatus.PUBLISHED
        await self.db.commit()
        await self.db.refresh(product)
        return product

    @staticmethod
    def _build_product(payload: ProductCreateRequest) -> MerchantProduct:
        data = payload.model_dump()
        data["status"] = payload.status.value
        data["in_app_price"] = data.pop("price")
        data["has_simulafly_listing"] = payload.status == ProductStatus.PUBLISHED
        return MerchantProduct(**data)

    async def _ensure_merchants_exist(self, merchant_ids: set[uuid.UUID]) -> None:
        if not merchant_ids:
            return
        existing = set(
            (
                await self.db.execute(
                    select(Merchant.id).where(Merchant.id.in_(merchant_ids))
                )
            ).scalars().all()
        )
        missing = merchant_ids - existing
        if missing:
            raise HTTPException(
                status_code=http_status.HTTP_404_NOT_FOUND,
                detail=f"merchant not found: {next(iter(missing))}",
            )

    async def _resolve_merchant_names(
        self,
        merchant_ids: set[uuid.UUID],
    ) -> dict[uuid.UUID, str]:
        """Batch-resolve merchant display names for the given ids."""
        if not merchant_ids:
            return {}
        rows = (
            await self.db.execute(
                select(Merchant.id, Merchant.display_name).where(
                    Merchant.id.in_(merchant_ids)
                )
            )
        ).all()
        return {row[0]: row[1] for row in rows}
