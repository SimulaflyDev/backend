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

``MerchantProduct`` has no soft-delete column, so no soft-delete exclusion is
applied here.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal

from fastapi import HTTPException, status as http_status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.merchant import Merchant
from app.models.merchant_product import MerchantProduct, ProductStatus
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
    title: str
    merchant_id: uuid.UUID
    merchant_name: str | None
    price: Decimal | None
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
                title=product.title,
                merchant_id=product.merchant_id,
                merchant_name=merchant_names.get(product.merchant_id),
                price=product.in_app_price,
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
        if new_status == ProductStatus.PUBLISHED:
            product.has_simulafly_listing = True
        elif new_status == ProductStatus.ARCHIVED:
            product.has_simulafly_listing = False
        await self.db.commit()
        await self.db.refresh(product)
        return product

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
