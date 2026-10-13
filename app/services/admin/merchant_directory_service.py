"""Merchant directory service — list, detail, and status actions (Requirement 8).

Backs ``app/routers/admin/merchants.py``. This module is intentionally separate
from the (concurrently developed) ``directory_service.py`` so the merchant
directory can be implemented without parallel-edit conflicts. It reuses the
shared listing engine (:func:`app.services.admin.listing.paginate`) for the
paginated/searchable/filterable/sortable listing (R8.1, R8.2, R8.3) and the
existing :class:`app.models.merchant.Merchant`, :class:`MerchantMember`, and
:class:`app.models.wallet.Wallet` models without modifying consumer/merchant
behavior.

Operations:

* :meth:`MerchantDirectoryService.list_merchants` — paginated listing with
  display/legal-name search, status/KYC filters, and whitelisted sort.
* :meth:`MerchantDirectoryService.get_merchant` — detail record including
  members (with the linked user's email/name) and a wallet summary; ``404``
  when the merchant id does not exist (R8.7).
* :meth:`MerchantDirectoryService.set_status` — set the merchant's status,
  used by the suspend (``suspended``) and activate (``active``) endpoints
  (R8.5, R8.6); ``404`` when missing.
"""

from __future__ import annotations

import uuid

from fastapi import HTTPException, status
from sqlalchemy import String, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from sqlalchemy.sql.elements import ColumnElement

from app.models.merchant import Merchant, MerchantMember, MerchantStatus
from app.models.merchant_verification import GstinVerification, PanVerification
from app.models.wallet import Wallet
from app.schemas.admin.merchants import MerchantShopCreateRequest
from app.services.admin.listing import ListParams, Page, paginate
from app.utils.id_generator import generate_mpuid, generate_mpsuid
from app.utils.slug import make_unique_slug, slugify_base


_STATE_EXPR = func.coalesce(
    Merchant.settings["onboarding_data"]["state"].as_string(),
    Merchant.settings["onboarding_submission"]["shop"]["shop_address"]["state"].as_string(),
)
_CITY_EXPR = func.coalesce(
    Merchant.settings["onboarding_data"]["city"].as_string(),
    Merchant.settings["onboarding_submission"]["shop"]["shop_address"]["city"].as_string(),
)

#: Whitelisted sortable columns for the merchant directory (R8 / R20.4).
#: Also used by the listing engine to resolve string *filter* keys to columns,
#: so any column we filter by (e.g. ``partner_id``) must be listed here.
_SORTABLE: dict[str, ColumnElement] = {
    "display_name": Merchant.display_name,
    "legal_name": Merchant.legal_name,
    "status": Merchant.status,
    "is_kyc_completed": Merchant.is_kyc_completed,
    "created_at": Merchant.created_at,
    "partner_id": Merchant.partner_id,
    "shop_id": Merchant.shop_id,
    "state": _STATE_EXPR,
    "city": _CITY_EXPR,
}

#: Role string identifying the owning member of a merchant (shop).
_OWNER_ROLE = "owner"

#: Columns matched (ILIKE OR) by the free-text search term (R8.2).
_SEARCHABLE: tuple[ColumnElement, ...] = (
    Merchant.display_name,
    Merchant.legal_name,
    _STATE_EXPR,
    _CITY_EXPR,
)


class MerchantDirectoryService:
    """List/detail/status operations over :class:`Merchant`."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def list_merchants(
        self,
        *,
        page: int = 1,
        page_size: int | None = None,
        search: str | None = None,
        sort: str | None = None,
        status_filter: str | None = None,
        is_kyc_completed: bool | None = None,
        partner_id: str | None = None,
        owner_user_id: uuid.UUID | None = None,
        primary_only: bool = False,
        state_filter: str | None = None,
        city_filter: str | None = None,
    ) -> Page:
        """Return a page of merchants (R8.1, R8.2, R8.3).

        Search matches display name or legal name (R8.2). The ``status_filter``,
        ``is_kyc_completed``, and ``partner_id`` filters are conjunctive (R8.3).
        ``owner_user_id`` narrows the listing to every shop owned by a single
        user (each merchant row is a shop; one owner may run several).
        ``primary_only`` collapses the listing to ONE row per partner — the
        oldest (primary) shop of each ``partner_id`` — so the merchant
        directory shows each partner once instead of repeating its shops.
        Sorting is restricted to the whitelisted columns; an unsupported field
        yields HTTP 422 via the shared listing engine.
        """
        params = ListParams(
            page=page,
            page_size=page_size if page_size is not None else 1,
            search=search,
            sort=sort,
            filters={
                "status": status_filter,
                "is_kyc_completed": is_kyc_completed,
                "partner_id": partner_id,
            },
        )
        # When the caller does not specify a page size, fall back to the
        # ListParams default rather than the placeholder above.
        if page_size is None:
            params.page_size = ListParams().page_size

        # Base statement; when filtering by owner we join the owning membership
        # so we list every shop run by that user (grouping key for shops).
        base_stmt = select(Merchant)
        if state_filter and state_filter.strip():
            base_stmt = base_stmt.where(
                func.lower(_STATE_EXPR) == state_filter.strip().lower()
            )
        if city_filter and city_filter.strip():
            base_stmt = base_stmt.where(
                func.lower(_CITY_EXPR) == city_filter.strip().lower()
            )
        if owner_user_id is not None:
            base_stmt = base_stmt.join(
                MerchantMember, MerchantMember.merchant_id == Merchant.id
            ).where(
                MerchantMember.user_id == owner_user_id,
                MerchantMember.role == _OWNER_ROLE,
            )

        # Collapse to one row per partner: keep only the oldest shop of each
        # partner group. Shops with no partner_id are their own group (keyed by
        # id), so they each still appear exactly once.
        if primary_only:
            group_expr = func.coalesce(Merchant.partner_id, cast(Merchant.id, String))
            primary_ids = (
                select(Merchant.id)
                .distinct(group_expr)
                .order_by(group_expr, Merchant.created_at.asc())
            )
            base_stmt = base_stmt.where(Merchant.id.in_(primary_ids))

        return await paginate(
            self.db,
            base_stmt,
            params=params,
            sortable=_SORTABLE,
            searchable=_SEARCHABLE,
        )

    async def get_merchant(
        self, merchant_id: uuid.UUID
    ) -> tuple[Merchant, Wallet | None, PanVerification | None, GstinVerification | None]:
        """Load a merchant with its members and wallet summary (R8.4).

        Members are eagerly loaded together with their linked user so the detail
        projection can include each member's email and name. The merchant's
        wallet (one per merchant) is returned alongside, or ``None`` when the
        merchant has no wallet. Raises ``404`` when the merchant id is unknown
        (R8.7).
        """
        merchant = await self._get_or_404(merchant_id, load_members=True)
        wallet = (
            await self.db.execute(
                select(Wallet).where(Wallet.merchant_id == merchant_id)
            )
        ).scalar_one_or_none()
        owner = next((member for member in merchant.members if member.role == _OWNER_ROLE), None)
        pan = None
        if owner is not None:
            pan = (
                await self.db.execute(
                    select(PanVerification).where(PanVerification.user_id == owner.user_id)
                )
            ).scalar_one_or_none()
        gstin = (
            await self.db.execute(
                select(GstinVerification).where(GstinVerification.merchant_id == merchant_id)
            )
        ).scalar_one_or_none()
        return merchant, wallet, pan, gstin

    async def create_shop(
        self, merchant_id: uuid.UUID, payload: MerchantShopCreateRequest
    ) -> Merchant:
        """Create an unverified shop for the selected merchant owner."""
        source = await self._get_or_404(merchant_id, load_members=True)
        owner = next((member for member in source.members if member.role == _OWNER_ROLE), None)
        if owner is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="merchant has no owner account",
            )

        partner_id = source.partner_id or await generate_mpuid(
            self.db, state_code=payload.state_code, city_code=payload.city_code
        )
        shop_id = await generate_mpsuid(
            self.db, partner_id=partner_id, city_code=payload.city_code
        )
        base = slugify_base(payload.legal_name or payload.display_name)
        existing_slugs = set(
            (
                await self.db.execute(
                    select(Merchant.slug).where(Merchant.slug.like(f"{base}%"))
                )
            ).scalars().all()
        )
        slug = make_unique_slug(
            payload.legal_name or payload.display_name,
            lambda candidate: candidate in existing_slugs,
        )
        referral_code = f"SIMULA-{uuid.uuid4().hex[:12].upper()}"
        settings = {
            "admin_created": True,
            "onboarding_completed": False,
            "onboarding_data": {"state": payload.state.strip(), "city": payload.city.strip()},
        }
        merchant = Merchant(
            partner_id=partner_id,
            shop_id=shop_id,
            slug=slug,
            legal_name=payload.legal_name or source.legal_name,
            display_name=payload.display_name,
            support_email=str(payload.support_email) if payload.support_email else source.support_email,
            support_phone=payload.support_phone or source.support_phone,
            country=source.country,
            status=MerchantStatus.TRIAL.value,
            settings=settings,
            referral_code=referral_code,
            address=payload.address,
            latitude=payload.latitude,
            longitude=payload.longitude,
            range_km=payload.range_km,
            is_kyc_completed=False,
            kyc_bonus_paid=source.kyc_bonus_paid,
        )
        self.db.add(merchant)
        await self.db.flush()
        self.db.add(
            MerchantMember(
                merchant_id=merchant.id,
                user_id=owner.user_id,
                role=_OWNER_ROLE,
            )
        )
        self.db.add(Wallet(merchant_id=merchant.id))
        await self.db.commit()
        await self.db.refresh(merchant)
        return merchant

    @staticmethod
    def location_for(merchant: Merchant) -> tuple[str | None, str | None]:
        settings = merchant.settings if isinstance(merchant.settings, dict) else {}
        onboarding = settings.get("onboarding_data")
        if not isinstance(onboarding, dict):
            onboarding = {}
        submission = settings.get("onboarding_submission")
        if not isinstance(submission, dict):
            submission = {}
        shop = submission.get("shop") if isinstance(submission.get("shop"), dict) else {}
        shop_address = (
            shop.get("shop_address") if isinstance(shop.get("shop_address"), dict) else {}
        )
        state = onboarding.get("state") or shop_address.get("state")
        city = onboarding.get("city") or shop_address.get("city")
        return (
            str(state).strip() if state else None,
            str(city).strip() if city else None,
        )

    async def set_status(self, merchant_id: uuid.UUID, new_status: str) -> Merchant:
        """Set a merchant's status and return the updated row (R8.5, R8.6).

        Raises ``404`` when the merchant id is unknown (R8.7). The caller (the
        router) is responsible for recording the audit entry via ``audited(...)``.
        """
        merchant = await self._get_or_404(merchant_id)
        merchant.status = new_status
        await self.db.commit()
        await self.db.refresh(merchant)
        return merchant

    async def suspend(self, merchant_id: uuid.UUID) -> Merchant:
        """Suspend a merchant (status → ``suspended``) (R8.5)."""
        return await self.set_status(merchant_id, MerchantStatus.SUSPENDED.value)

    async def activate(self, merchant_id: uuid.UUID) -> Merchant:
        """Activate a merchant (status → ``active``) (R8.6)."""
        return await self.set_status(merchant_id, MerchantStatus.ACTIVE.value)

    # -- Internal helpers --------------------------------------------------

    async def _get_or_404(
        self,
        merchant_id: uuid.UUID,
        *,
        load_members: bool = False,
    ) -> Merchant:
        stmt = select(Merchant).where(Merchant.id == merchant_id)
        if load_members:
            stmt = stmt.options(
                selectinload(Merchant.members).selectinload(MerchantMember.user)
            )
        merchant = (await self.db.execute(stmt)).scalar_one_or_none()
        if merchant is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="merchant not found",
            )
        return merchant
