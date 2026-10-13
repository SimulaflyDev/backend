from decimal import Decimal

import pytest

from app.models.merchant import Merchant, MerchantMember, MemberRole
from app.models.merchant_product import ProductStatus
from app.models.merchant_verification import GstinVerification, PanVerification
from app.schemas.admin.merchants import MerchantShopCreateRequest
from app.schemas.admin.products import ProductCreateRequest, ProductUpdateRequest
from app.services.admin.merchant_directory_service import MerchantDirectoryService
from app.services.admin.product_service import ProductListParams, ProductService


def _merchant(**overrides) -> Merchant:
    values = {
        "partner_id": "SIM-M-MH-000777-M",
        "shop_id": "SIM-S-000777-01-M",
        "slug": "admin-catalog-source",
        "legal_name": "Admin Catalog Pvt Ltd",
        "display_name": "Admin Catalog",
        "referral_code": "ADMIN-CATALOG-REF",
        "settings": {"onboarding_data": {"state": "Maharashtra", "city": "Mumbai"}},
    }
    values.update(overrides)
    return Merchant(**values)


@pytest.mark.asyncio
async def test_admin_product_crud_bulk_and_soft_hide(db_session):
    merchant = _merchant()
    db_session.add(merchant)
    await db_session.commit()

    service = ProductService(db_session)
    product = await service.create_product(
        ProductCreateRequest(
            merchant_id=merchant.id,
            sku="ADMIN-001",
            title="Admin Chair",
            status=ProductStatus.PUBLISHED,
            price=Decimal("4999.00"),
            in_app_stock=8,
        )
    )
    assert product.status == "published"
    assert product.has_simulafly_listing is True

    updated = await service.update_product(
        product.id,
        ProductUpdateRequest(title="Admin Chair Pro", price=Decimal("5499.00")),
    )
    assert updated.title == "Admin Chair Pro"
    assert updated.in_app_price == Decimal("5499.00")

    archived = await service.archive_product(product.id)
    assert archived.status == "archived"
    assert archived.has_simulafly_listing is False

    created = await service.bulk_create_products(
        [
            ProductCreateRequest(merchant_id=merchant.id, sku="ADMIN-002", title="Desk"),
            ProductCreateRequest(merchant_id=merchant.id, sku="ADMIN-003", title="Lamp"),
        ]
    )
    assert [item.sku for item in created] == ["ADMIN-002", "ADMIN-003"]

    page = await service.list_products(ProductListParams(sort="title", page_size=10))
    assert page.total == 3


@pytest.mark.asyncio
async def test_admin_shop_creation_location_filter_and_kyc_detail(db_session, test_user):
    source = _merchant()
    db_session.add(source)
    await db_session.flush()
    db_session.add(
        MerchantMember(
            merchant_id=source.id,
            user_id=test_user.id,
            role=MemberRole.OWNER.value,
        )
    )
    db_session.add(
        PanVerification(
            user_id=test_user.id,
            pan_fingerprint="a" * 64,
            pan_last_four="F123",
            verified_name="Test Owner",
            category="individual",
        )
    )
    await db_session.commit()

    service = MerchantDirectoryService(db_session)
    shop = await service.create_shop(
        source.id,
        MerchantShopCreateRequest(
            display_name="Pune Shop",
            address="12 Market Road, Pune",
            state="Maharashtra",
            city="Pune",
            state_code="MH",
            city_code="P",
        ),
    )
    assert shop.partner_id == source.partner_id
    assert shop.shop_id.endswith("-02-M")
    assert shop.status == "trial"
    assert shop.is_kyc_completed is False

    db_session.add(
        GstinVerification(
            merchant_id=shop.id,
            gstin="27ABCDE1234F1Z5",
            legal_name="Admin Catalog Pvt Ltd",
            state_name="Maharashtra",
            state_code="27",
            registration_status="Active",
        )
    )
    await db_session.commit()

    page = await service.list_merchants(
        page_size=10,
        state_filter="maharashtra",
        city_filter="pune",
        sort="city",
    )
    assert [item.id for item in page.items] == [shop.id]

    merchant, wallet, pan, gstin = await service.get_merchant(shop.id)
    assert merchant.id == shop.id
    assert wallet is not None
    assert pan is not None and pan.pan_last_four == "F123"
    assert gstin is not None and gstin.gstin == "27ABCDE1234F1Z5"
