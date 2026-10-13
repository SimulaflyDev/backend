"""Request/response schemas for the admin product directory (R10).

These back the product directory/list (R10.1–R10.3), single-product detail
(R10.4), and the visibility-status mutation (R10.5). The listing reuses the
shared :class:`app.schemas.admin.listing.ListingEnvelope` pagination shape via
:data:`ProductListEnvelope`.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from app.models.merchant_product import ProductStatus
from app.schemas.admin.listing import ListingEnvelope


class ProductListItem(BaseModel):
    """A single row in the product directory listing (R10.1).

    Exposes the product ``id`` (identifier), ``title``, owning merchant
    (``merchant_id`` plus the merchant's ``merchant_name`` when resolvable),
    ``price`` (the in-app price), ``status``, and ``created_at`` (creation date).
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    sku: str
    title: str
    merchant_id: uuid.UUID
    merchant_name: str | None = None
    price: Decimal | None = None
    category: str | None = None
    primary_image_url: str | None = None
    in_app_stock: int | None = None
    status: str
    created_at: datetime


#: Pagination envelope for ``GET /admin/products`` (R10.1).
ProductListEnvelope = ListingEnvelope[ProductListItem]


class ProductDetail(BaseModel):
    """Full detail record for a single product (R10.4)."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    merchant_id: uuid.UUID
    merchant_name: str | None = None
    sku: str
    title: str
    description: str | None = None
    category: str | None = None
    subcategory: str | None = None
    brand: str | None = None
    status: str
    primary_image_url: str | None = None
    additional_images: list[str] = Field(default_factory=list)
    has_simulafly_listing: bool
    price: Decimal | None = None
    in_app_stock: int | None = None
    health_score: str
    health_reason: str | None = None
    created_at: datetime
    updated_at: datetime


class ProductStatusUpdateRequest(BaseModel):
    """Payload for ``PATCH /admin/products/{id}/status`` (R10.5).

    ``status`` must be a valid :class:`ProductStatus` value; the service
    persists it as the product's new visibility status and the action is
    audited.
    """

    status: ProductStatus


class ProductStatusResponse(BaseModel):
    """Result of a product visibility-status change (R10.5)."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    status: str


class ProductCreateRequest(BaseModel):
    """Admin payload for adding a product to a merchant shop."""

    merchant_id: uuid.UUID
    sku: str = Field(min_length=1, max_length=64)
    title: str = Field(min_length=1, max_length=500)
    description: str | None = None
    category: str | None = Field(default=None, max_length=255)
    subcategory: str | None = Field(default=None, max_length=255)
    brand: str | None = Field(default=None, max_length=255)
    status: ProductStatus = ProductStatus.DRAFT
    primary_image_url: str | None = Field(default=None, max_length=2048)
    additional_images: list[str] = Field(default_factory=list, max_length=4)
    price: Decimal | None = Field(default=None, ge=0, decimal_places=2)
    in_app_stock: int | None = Field(default=None, ge=0)


class ProductUpdateRequest(BaseModel):
    """Editable product fields. Omitted fields are left unchanged."""

    sku: str | None = Field(default=None, min_length=1, max_length=64)
    title: str | None = Field(default=None, min_length=1, max_length=500)
    description: str | None = None
    category: str | None = Field(default=None, max_length=255)
    subcategory: str | None = Field(default=None, max_length=255)
    brand: str | None = Field(default=None, max_length=255)
    status: ProductStatus | None = None
    primary_image_url: str | None = Field(default=None, max_length=2048)
    additional_images: list[str] | None = Field(default=None, max_length=4)
    price: Decimal | None = Field(default=None, ge=0, decimal_places=2)
    in_app_stock: int | None = Field(default=None, ge=0)


class ProductBulkCreateRequest(BaseModel):
    """Atomic bulk-add request, capped to keep admin actions bounded."""

    items: list[ProductCreateRequest] = Field(min_length=1, max_length=100)


class ProductBulkCreateResponse(BaseModel):
    created: int
    items: list[ProductDetail]
