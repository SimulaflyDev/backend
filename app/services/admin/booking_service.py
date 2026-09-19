"""Booking operations service (Requirement 11) — over the existing ``Order``.

``BookingService`` backs the admin ``bookings`` router. It reuses the shared
listing engine (:func:`app.services.admin.listing.paginate`) over the existing
:class:`app.models.lead.Order` model (extended with additive nullable columns:
``dispute_status``, ``dispute_reason``, ``dispute_resolution``,
``fulfillment_status``, ``deleted_at``) to provide:

* **Listing** — paginated bookings with conjunctive filters for status,
  merchant, and a created-at date range (R11.1, R11.2), whitelisted sort, and
  soft-delete exclusion.
* **Detail** — a single booking including its line items (from the ``items``
  JSONB array), fulfillment status, and dispute status (R11.3); a missing
  identifier raises HTTP 404 (R11.8).
* **Disputes queue** — bookings with ``dispute_status='open'`` (R11.4).
* **Resolve dispute** — persist a new dispute status + resolution outcome
  (R11.5). Auditing is performed at the router boundary via ``audited(...)``.
* **Fulfillment queue** — bookings with ``fulfillment_status='pending'`` (R11.6).
* **Update fulfillment** — persist a new fulfillment status (R11.7).

``Order`` has no ORM relationship to ``Merchant``/``User``, so the router
resolves customer/merchant display names via :meth:`resolve_names`, which
batches a single lookup per related table (mirroring
:meth:`DirectoryService.resolve_attributions`).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from app.models.lead import DisputeStatus, FulfillmentStatus, Order
from app.models.merchant import Merchant
from app.models.user import User
from app.services.admin.listing import ListParams, Page, paginate


class BookingService:
    """List / detail / dispute / fulfillment operations for bookings (R11)."""

    #: Whitelisted sortable (and string-filterable) fields → column expressions.
    SORTABLE: dict[str, ColumnElement] = {
        "status": Order.status,
        "merchant_id": Order.merchant_id,
        "total_estimated": Order.total_estimated,
        "dispute_status": Order.dispute_status,
        "fulfillment_status": Order.fulfillment_status,
        "created_at": Order.created_at,
    }

    #: Bookings carry no free-text columns worth ILIKE-matching; search is unused
    #: but the engine still supports it if a searchable column is added later.
    SEARCHABLE: list[ColumnElement] = []

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    # -- Listing -----------------------------------------------------------

    async def list_bookings(
        self,
        params: ListParams,
        *,
        order_status: str | None = None,
        merchant_id: uuid.UUID | None = None,
        created_from: datetime | None = None,
        created_to: datetime | None = None,
    ) -> Page:
        """Return a paginated page of bookings (R11.1, R11.2).

        ``order_status`` and ``merchant_id`` are applied as conjunctive equality
        filters; ``created_from`` / ``created_to`` bound the creation date range
        (inclusive lower, inclusive upper). Soft-deleted rows are excluded.
        """
        if order_status is not None:
            params.filters[Order.status] = order_status
        if merchant_id is not None:
            params.filters[Order.merchant_id] = merchant_id

        base_stmt = select(Order)
        if created_from is not None:
            base_stmt = base_stmt.where(Order.created_at >= created_from)
        if created_to is not None:
            base_stmt = base_stmt.where(Order.created_at <= created_to)

        return await paginate(
            self.db,
            base_stmt,
            params=params,
            sortable=self.SORTABLE,
            searchable=self.SEARCHABLE,
            soft_delete_col=Order.deleted_at,
        )

    async def list_disputes(self, params: ListParams) -> Page:
        """Return bookings with an open dispute (``dispute_status='open'``, R11.4)."""
        base_stmt = select(Order).where(
            Order.dispute_status == DisputeStatus.OPEN.value
        )
        return await paginate(
            self.db,
            base_stmt,
            params=params,
            sortable=self.SORTABLE,
            searchable=self.SEARCHABLE,
            soft_delete_col=Order.deleted_at,
        )

    async def list_fulfillment_queue(self, params: ListParams) -> Page:
        """Return bookings pending fulfillment (``fulfillment_status='pending'``, R11.6)."""
        base_stmt = select(Order).where(
            Order.fulfillment_status == FulfillmentStatus.PENDING.value
        )
        return await paginate(
            self.db,
            base_stmt,
            params=params,
            sortable=self.SORTABLE,
            searchable=self.SEARCHABLE,
            soft_delete_col=Order.deleted_at,
        )

    # -- Detail ------------------------------------------------------------

    async def get_booking(self, booking_id: uuid.UUID) -> Order:
        """Fetch a single non-deleted booking, or raise HTTP 404 (R11.3, R11.8)."""
        stmt = select(Order).where(
            Order.id == booking_id, Order.deleted_at.is_(None)
        )
        order = (await self.db.execute(stmt)).scalar_one_or_none()
        if order is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="booking not found",
            )
        return order

    # -- Mutations ---------------------------------------------------------

    async def resolve_dispute(
        self,
        booking_id: uuid.UUID,
        *,
        resolution: str,
        new_status: DisputeStatus = DisputeStatus.RESOLVED,
    ) -> Order:
        """Persist a dispute resolution outcome and status (R11.5).

        A missing booking raises HTTP 404. The router wraps this in
        ``audited(...)`` so the resolution is recorded.
        """
        order = await self.get_booking(booking_id)
        order.dispute_status = new_status.value
        order.dispute_resolution = resolution
        await self.db.commit()
        await self.db.refresh(order)
        return order

    async def update_fulfillment(
        self,
        booking_id: uuid.UUID,
        new_status: FulfillmentStatus,
    ) -> Order:
        """Persist a new fulfillment status for the booking (R11.7).

        A missing booking raises HTTP 404 (R11.8).
        """
        from app.models.lead import BuyerLead
        from app.services.order_lifecycle import apply_order_update

        order = (await self.db.execute(select(Order).where(
            Order.id == booking_id, Order.deleted_at.is_(None),
        ).with_for_update())).scalar_one_or_none()
        if order is None:
            raise HTTPException(status_code=404, detail="Booking not found")
        lead = (await self.db.execute(select(BuyerLead).where(
            BuyerLead.id == order.lead_id,
        ).with_for_update())).scalar_one()
        await apply_order_update(self.db, order, lead, fulfillment_status=new_status.value)
        await self.db.commit()
        await self.db.refresh(order)
        return order

    # -- Name resolution ---------------------------------------------------

    async def resolve_names(
        self, orders: list[Order]
    ) -> tuple[dict[uuid.UUID, str | None], dict[uuid.UUID, str | None]]:
        """Resolve customer and merchant display names for a set of bookings.

        ``Order`` has no ORM relationship to ``User``/``Merchant``, so this
        batches one lookup per table and returns ``(customer_names,
        merchant_names)`` keyed by the related id. Missing references map to
        ``None`` rather than raising.
        """
        user_ids = {o.user_id for o in orders}
        merchant_ids = {o.merchant_id for o in orders}

        customer_names: dict[uuid.UUID, str | None] = {}
        merchant_names: dict[uuid.UUID, str | None] = {}

        if user_ids:
            rows = (
                await self.db.execute(
                    select(User.id, User.full_name).where(User.id.in_(user_ids))
                )
            ).all()
            customer_names = {row[0]: row[1] for row in rows}

        if merchant_ids:
            rows = (
                await self.db.execute(
                    select(Merchant.id, Merchant.display_name).where(
                        Merchant.id.in_(merchant_ids)
                    )
                )
            ).all()
            merchant_names = {row[0]: row[1] for row in rows}

        return customer_names, merchant_names
