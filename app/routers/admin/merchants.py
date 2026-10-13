"""Admin merchant directory router (Requirement 8).

Endpoints (all prefixed ``/admin``; mounted under ``/api/v1`` by the wiring
step in task 16.1):

| Method | Path                          | Permission         | Req       |
|--------|-------------------------------|--------------------|-----------|
| GET    | `/merchants`                  | `merchants.read`   | 8.1–8.3   |
| GET    | `/merchants/{id}`             | `merchants.read`   | 8.4, 8.7  |
| POST   | `/merchants/{id}/suspend`     | `merchants.suspend`| 8.5, 8.7  |
| POST   | `/merchants/{id}/activate`    | `merchants.suspend`| 8.6, 8.7  |

Reads are gated by ``require_permission("merchants.read")``; the suspend and
activate actions by ``require_permission("merchants.suspend")``. Both mutating
routes are wrapped with ``audited(...)`` so each writes one immutable audit
entry (Requirement 19.1 / R8.5, R8.6). Missing merchant identifiers yield
HTTP 404 from the service layer (R8.7).
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.models.merchant import Merchant, MerchantStatus
from app.models.merchant_verification import GstinVerification, PanVerification
from app.models.wallet import Wallet
from app.schemas.admin.listing import ListingEnvelope
from app.schemas.admin.merchants import (
    MerchantDetail,
    MerchantListItem,
    MerchantMemberOut,
    MerchantKycDetail,
    MerchantShopCreateRequest,
    MerchantStatusResponse,
    GstinKycDetail,
    PanKycDetail,
    WalletSummary,
)
from app.services.admin.audit_service import AuditContext, audited
from app.services.admin.merchant_directory_service import MerchantDirectoryService
from app.utils.admin_dependencies import require_permission

router = APIRouter(prefix="/admin", tags=["admin-merchants"])

DBSession = Annotated[AsyncSession, Depends(get_db)]


def _to_status_response(merchant: Merchant) -> MerchantStatusResponse:
    return MerchantStatusResponse(
        id=merchant.id,
        display_name=merchant.display_name,
        status=merchant.status,
    )


def _to_list_item(merchant: Merchant) -> MerchantListItem:
    state, city = MerchantDirectoryService.location_for(merchant)
    return MerchantListItem(
        id=merchant.id,
        partner_id=merchant.partner_id,
        shop_id=merchant.shop_id,
        display_name=merchant.display_name,
        legal_name=merchant.legal_name,
        status=merchant.status,
        is_kyc_completed=merchant.is_kyc_completed,
        state=state,
        city=city,
        created_at=merchant.created_at,
    )


def _to_detail(
    merchant: Merchant,
    wallet: Wallet | None,
    pan: PanVerification | None,
    gstin: GstinVerification | None,
) -> MerchantDetail:
    members = [
        MerchantMemberOut(
            id=member.id,
            user_id=member.user_id,
            role=member.role,
            email=member.user.email if member.user is not None else None,
            full_name=member.user.full_name if member.user is not None else None,
            joined_at=member.joined_at,
        )
        for member in merchant.members
    ]
    state, city = MerchantDirectoryService.location_for(merchant)
    return MerchantDetail(
        id=merchant.id,
        partner_id=merchant.partner_id,
        shop_id=merchant.shop_id,
        slug=merchant.slug,
        display_name=merchant.display_name,
        legal_name=merchant.legal_name,
        status=merchant.status,
        is_kyc_completed=merchant.is_kyc_completed,
        country=merchant.country,
        address=merchant.address,
        state=state,
        city=city,
        latitude=merchant.latitude,
        longitude=merchant.longitude,
        range_km=merchant.range_km,
        support_email=merchant.support_email,
        support_phone=merchant.support_phone,
        created_at=merchant.created_at,
        updated_at=merchant.updated_at,
        members=members,
        wallet=WalletSummary.model_validate(wallet) if wallet is not None else None,
        kyc=MerchantKycDetail(
            completed=merchant.is_kyc_completed,
            pan=(
                PanKycDetail(
                    masked_pan=f"******{pan.pan_last_four}",
                    verified_name=pan.verified_name,
                    category=pan.category,
                    provider_transaction_id=pan.provider_transaction_id,
                    verified_at=pan.verified_at,
                )
                if pan is not None
                else None
            ),
            gstin=(
                GstinKycDetail(
                    gstin=gstin.gstin,
                    legal_name=gstin.legal_name,
                    business_nature=gstin.business_nature,
                    state_name=gstin.state_name,
                    state_code=gstin.state_code,
                    registration_status=gstin.registration_status,
                    registration_start_date=gstin.registration_start_date,
                    provider_transaction_id=gstin.provider_transaction_id,
                    verified_at=gstin.verified_at,
                )
                if gstin is not None
                else None
            ),
        ),
    )


@router.get(
    "/merchants",
    response_model=ListingEnvelope[MerchantListItem],
    dependencies=[Depends(require_permission("merchants.read"))],
)
async def list_merchants(
    db: DBSession,
    page: int = Query(default=1, ge=1),
    page_size: int | None = Query(default=None, ge=1),
    search: str | None = Query(default=None),
    sort: str | None = Query(default=None),
    status: str | None = Query(default=None, description="Filter by merchant status"),
    is_kyc_completed: bool | None = Query(
        default=None, description="Filter by KYC completion"
    ),
    partner_id: str | None = Query(
        default=None, description="Filter by partner id (all shops of one partner)"
    ),
    owner_user_id: uuid.UUID | None = Query(
        default=None, description="Filter to every shop owned by this user"
    ),
    primary_only: bool = Query(
        default=False, description="Collapse to one row per partner (primary shop)"
    ),
    state: str | None = Query(default=None, description="Filter by shop state"),
    city: str | None = Query(default=None, description="Filter by shop city"),
) -> ListingEnvelope[MerchantListItem]:
    """Paginated, searchable, filterable merchant directory (R8.1, R8.2, R8.3)."""
    page_obj = await MerchantDirectoryService(db).list_merchants(
        page=page,
        page_size=page_size,
        search=search,
        sort=sort,
        status_filter=status,
        is_kyc_completed=is_kyc_completed,
        partner_id=partner_id,
        owner_user_id=owner_user_id,
        primary_only=primary_only,
        state_filter=state,
        city_filter=city,
    )
    return ListingEnvelope[MerchantListItem](
        items=[_to_list_item(m) for m in page_obj.items],
        page=page_obj.page,
        page_size=page_obj.page_size,
        total=page_obj.total,
        total_pages=page_obj.total_pages,
        has_next=page_obj.has_next,
        next_page=page_obj.next_page,
    )


@router.get(
    "/merchants/{merchant_id}",
    response_model=MerchantDetail,
    dependencies=[Depends(require_permission("merchants.read"))],
)
async def get_merchant(
    merchant_id: uuid.UUID,
    db: DBSession,
) -> MerchantDetail:
    """Single merchant detail incl. members, wallet summary, status (R8.4, R8.7)."""
    merchant, wallet, pan, gstin = await MerchantDirectoryService(db).get_merchant(merchant_id)
    return _to_detail(merchant, wallet, pan, gstin)


@router.post(
    "/merchants/{merchant_id}/shops",
    response_model=MerchantListItem,
    status_code=201,
    dependencies=[Depends(require_permission("stores.manage"))],
)
async def create_merchant_shop(
    merchant_id: uuid.UUID,
    payload: MerchantShopCreateRequest,
    db: DBSession,
    audit: Annotated[AuditContext, Depends(audited("stores.create", "merchant"))],
) -> MerchantListItem:
    merchant = await MerchantDirectoryService(db).create_shop(merchant_id, payload)
    audit.set_target(merchant.id)
    audit.add_metadata(parent_merchant_id=str(merchant_id), partner_id=merchant.partner_id)
    return _to_list_item(merchant)


@router.post(
    "/merchants/{merchant_id}/suspend",
    response_model=MerchantStatusResponse,
    dependencies=[Depends(require_permission("merchants.suspend"))],
)
async def suspend_merchant(
    merchant_id: uuid.UUID,
    db: DBSession,
    audit: Annotated[AuditContext, Depends(audited("merchants.suspend", "merchant"))],
) -> MerchantStatusResponse:
    """Suspend a merchant (status → ``suspended``); audited (R8.5, R8.7)."""
    merchant = await MerchantDirectoryService(db).suspend(merchant_id)
    audit.set_target(merchant_id)
    audit.add_metadata(status=MerchantStatus.SUSPENDED.value)
    return _to_status_response(merchant)


@router.post(
    "/merchants/{merchant_id}/activate",
    response_model=MerchantStatusResponse,
    dependencies=[Depends(require_permission("merchants.suspend"))],
)
async def activate_merchant(
    merchant_id: uuid.UUID,
    db: DBSession,
    audit: Annotated[AuditContext, Depends(audited("merchants.activate", "merchant"))],
) -> MerchantStatusResponse:
    """Activate a merchant (status → ``active``); audited (R8.6, R8.7)."""
    merchant = await MerchantDirectoryService(db).activate(merchant_id)
    audit.set_target(merchant_id)
    audit.add_metadata(status=MerchantStatus.ACTIVE.value)
    return _to_status_response(merchant)
