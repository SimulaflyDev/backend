import uuid

import pytest
from sqlalchemy import select


@pytest.mark.asyncio
async def test_create_merchant_persists_required_fields(db_session, test_user):
    from app.models.merchant import Merchant, MerchantMember, MemberRole

    m = Merchant(
        slug="acme-furniture",
        legal_name="Acme Furniture Co.",
        display_name="Acme",
        country="IN",
        referral_code="SIMULA-ACME-2026",
    )
    db_session.add(m)
    await db_session.commit()
    await db_session.refresh(m)

    assert isinstance(m.id, uuid.UUID)
    assert m.status == "active"
    assert m.settings == {}
    assert m.created_at is not None


@pytest.mark.asyncio
async def test_merchant_member_unique_per_merchant_user_pair(db_session, test_user):
    from app.models.merchant import Merchant, MerchantMember, MemberRole

    m = Merchant(
        slug="dup-test",
        legal_name="Dup",
        display_name="Dup",
        referral_code="DUP-1",
    )
    db_session.add(m)
    await db_session.commit()

    mm1 = MerchantMember(merchant_id=m.id, user_id=test_user.id, role=MemberRole.OWNER.value)
    db_session.add(mm1)
    await db_session.commit()

    mm2 = MerchantMember(merchant_id=m.id, user_id=test_user.id, role=MemberRole.STAFF.value)
    db_session.add(mm2)
    with pytest.raises(Exception):  # IntegrityError from UNIQUE constraint
        await db_session.commit()
    await db_session.rollback()


def test_merchant_create_validates_required_fields():
    from app.schemas.merchant import MerchantCreate
    from pydantic import ValidationError

    # Valid input
    valid = MerchantCreate(legal_name="Acme Furniture Co.", display_name="Acme")
    assert valid.country == "IN"  # default
    assert valid.support_email is None

    # Missing required field
    with pytest.raises(ValidationError):
        MerchantCreate(legal_name="Acme")  # missing display_name

    storefront = MerchantCreate(
        legal_name="Storefront Ltd",
        display_name="Storefront",
        settings={
            "storefront": {
                "tagline": "Live better, every day.",
                "hero_image_url": "https://example.com/cover.webp",
                "featured_categories": ["Living Room", "Bedroom"],
            }
        },
    )
    assert storefront.settings["storefront"]["tagline"] == "Live better, every day."

    with pytest.raises(ValidationError):
        MerchantCreate(
            legal_name="Too Many Ltd",
            display_name="Too Many",
            settings={"storefront": {"featured_categories": ["One", "Two", "Three", "Four"]}},
        )


def test_merchant_member_invite_validates_email_and_role():
    from app.schemas.merchant import MemberInvite
    from pydantic import ValidationError

    valid = MemberInvite(email="sarah@acme.com", role="admin")
    assert valid.role == "admin"

    with pytest.raises(ValidationError):
        MemberInvite(email="not-an-email", role="admin")

    with pytest.raises(ValidationError):
        MemberInvite(email="ok@acme.com", role="not-a-role")


@pytest.mark.asyncio
async def test_onboarding_creates_merchant_with_owner_membership(auth_client, test_user, db_session):
    from app.models.merchant import Merchant, MerchantMember

    r = await auth_client.post(
        "/api/v1/merchants/",
        json={"legal_name": "Acme Furniture Co.", "display_name": "Acme"},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["legal_name"] == "Acme Furniture Co."
    assert body["slug"] == "acme-furniture-co"
    assert body["referral_code"].startswith("SIMULA-")

    # Owner membership row created
    res = await db_session.execute(select(MerchantMember).where(MerchantMember.user_id == test_user.id))
    members = list(res.scalars().all())
    assert len(members) == 1
    assert members[0].role == "owner"


@pytest.mark.asyncio
async def test_onboarding_handles_slug_collision(auth_client, test_user, db_session):
    # Create first merchant
    r1 = await auth_client.post(
        "/api/v1/merchants/",
        json={"legal_name": "Acme Furniture Co.", "display_name": "Acme 1"},
    )
    assert r1.status_code == 201
    slug1 = r1.json()["slug"]

    # Create second with same legal_name — must produce a different slug
    r2 = await auth_client.post(
        "/api/v1/merchants/",
        json={"legal_name": "Acme Furniture Co.", "display_name": "Acme 2"},
    )
    assert r2.status_code == 201
    slug2 = r2.json()["slug"]
    assert slug1 != slug2
    assert slug2.startswith("acme-furniture-co-")


@pytest.mark.asyncio
async def test_public_merchant_returns_safe_storefront_settings(auth_client, verify_merchant):
    created = await auth_client.post(
        "/api/v1/merchants/",
        json={
            "legal_name": "Public Store Ltd",
            "display_name": "Public Store",
            "settings": {
                "onboarding_data": {
                    "description": "Furniture for thoughtful homes.",
                    "categories": ["Living Room", "Lighting"],
                },
                "storefront": {
                    "tagline": "Live beautifully.",
                    "hero_image_url": "https://example.com/hero.webp",
                    "featured_categories": ["Lighting"],
                },
                "security": {"allowed_ips": "10.0.0.1"},
                "notifications": {"email_new_order": True},
            },
        },
    )
    assert created.status_code == 201, created.text
    await verify_merchant(created.json()["id"])

    response = await auth_client.get(
        f"/api/v1/merchants/public/{created.json()['shop_id']}"
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert "settings" not in body
    assert body["is_kyc_completed"] is True
    assert body["shop_id"] == created.json()["shop_id"]
    assert body["partner_id"] == created.json()["partner_id"]
    assert "security" not in body
    assert "notifications" not in body
    assert body["storefront"] == {
        "tagline": "Live beautifully.",
        "description": "Furniture for thoughtful homes.",
        "hero_image_url": "https://example.com/hero.webp",
        "categories": ["Living Room", "Lighting"],
        "featured_categories": ["Lighting"],
    }


@pytest.mark.asyncio
async def test_list_my_merchants_returns_only_my_memberships(auth_client, test_user):
    # Create two merchants under this user
    for name in ("First Org", "Second Org"):
        r = await auth_client.post(
            "/api/v1/merchants/", json={"legal_name": name, "display_name": name}
        )
        assert r.status_code == 201

    r = await auth_client.get("/api/v1/merchants/me")
    assert r.status_code == 200
    body = r.json()
    names = sorted(m["legal_name"] for m in body)
    assert names == ["First Org", "Second Org"]


@pytest.mark.asyncio
async def test_get_merchant_requires_membership(auth_client, db_session):
    from app.models.merchant import Merchant

    foreign = Merchant(
        slug="foreign", legal_name="Foreign", display_name="Foreign", referral_code="FOR-1"
    )
    db_session.add(foreign)
    await db_session.commit()
    await db_session.refresh(foreign)

    r = await auth_client.get(
        f"/api/v1/merchants/{foreign.id}", headers={"X-Merchant-Id": str(foreign.id)}
    )
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_patch_merchant_requires_owner_or_admin(auth_client, test_user, db_session):
    from app.models.merchant import Merchant, MerchantMember, MemberRole

    m = Merchant(slug="patch", legal_name="Patch", display_name="Patch", referral_code="PCH-1", is_kyc_completed=True)
    db_session.add(m)
    await db_session.commit()
    await db_session.refresh(m)

    # Staff role — not allowed to PATCH
    db_session.add(MerchantMember(merchant_id=m.id, user_id=test_user.id, role=MemberRole.STAFF.value))
    await db_session.commit()

    r = await auth_client.patch(
        f"/api/v1/merchants/{m.id}",
        headers={"X-Merchant-Id": str(m.id)},
        json={"display_name": "New Name"},
    )
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_list_members_returns_all_members_with_email(auth_client, test_user, db_session, verify_merchant):
    r = await auth_client.post(
        "/api/v1/merchants/", json={"legal_name": "Team Test", "display_name": "Team"}
    )
    mid = r.json()["id"]
    await verify_merchant(mid)

    r = await auth_client.get(
        f"/api/v1/merchants/{mid}/members", headers={"X-Merchant-Id": mid}
    )
    assert r.status_code == 200
    members = r.json()
    assert len(members) == 1
    assert members[0]["email"] == test_user.email
    assert members[0]["role"] == "owner"


@pytest.mark.asyncio
async def test_invite_existing_user_adds_membership(auth_client, test_user, db_session, verify_merchant):
    from app.core.security import hash_password
    from app.models.user import User

    invitee = User(email="invitee@example.com", hashed_password=hash_password("password123"))
    db_session.add(invitee)
    await db_session.commit()

    r = await auth_client.post(
        "/api/v1/merchants/", json={"legal_name": "Inv Test", "display_name": "Inv"}
    )
    mid = r.json()["id"]
    await verify_merchant(mid)

    r = await auth_client.post(
        f"/api/v1/merchants/{mid}/members/invite",
        headers={"X-Merchant-Id": mid},
        json={"email": "invitee@example.com", "role": "admin"},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["email"] == "invitee@example.com"
    assert body["role"] == "admin"


@pytest.mark.asyncio
async def test_invite_unknown_email_returns_404(auth_client, verify_merchant):
    r = await auth_client.post(
        "/api/v1/merchants/", json={"legal_name": "Unk", "display_name": "Unk"}
    )
    mid = r.json()["id"]
    await verify_merchant(mid)

    r = await auth_client.post(
        f"/api/v1/merchants/{mid}/members/invite",
        headers={"X-Merchant-Id": mid},
        json={"email": "nobody@example.com", "role": "staff"},
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_change_role_requires_owner(auth_client, db_session, verify_merchant):
    from app.core.security import hash_password
    from app.models.user import User
    from app.models.merchant import Merchant, MerchantMember, MemberRole

    # Owner creates merchant; admin tries to change another member's role
    r = await auth_client.post(
        "/api/v1/merchants/", json={"legal_name": "Role Test", "display_name": "Role"}
    )
    mid = r.json()["id"]
    await verify_merchant(mid)

    other = User(email="other@example.com", hashed_password=hash_password("password123"))
    db_session.add(other)
    await db_session.commit()
    await db_session.refresh(other)

    # Add other as admin via invite
    await auth_client.post(
        f"/api/v1/merchants/{mid}/members/invite",
        headers={"X-Merchant-Id": mid},
        json={"email": "other@example.com", "role": "admin"},
    )

    # Sign in as `other` and try to demote the owner — should be 403
    from app.core.security import create_access_token
    other_token = create_access_token(str(other.id))
    from httpx import AsyncClient, ASGITransport
    from app.main import app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as cl:
        cl.headers.update({"Authorization": f"Bearer {other_token}", "X-Merchant-Id": mid})
        # Find the owner's user_id
        list_r = await cl.get(f"/api/v1/merchants/{mid}/members")
        owner_uid = next(m["user_id"] for m in list_r.json() if m["role"] == "owner")

        r = await cl.patch(
            f"/api/v1/merchants/{mid}/members/{owner_uid}",
            json={"role": "staff"},
        )
        assert r.status_code == 403


@pytest.mark.asyncio
async def test_remove_member_owner_cannot_remove_self(auth_client, test_user, verify_merchant):
    r = await auth_client.post(
        "/api/v1/merchants/", json={"legal_name": "Self", "display_name": "Self"}
    )
    mid = r.json()["id"]
    await verify_merchant(mid)

    r = await auth_client.delete(
        f"/api/v1/merchants/{mid}/members/{test_user.id}",
        headers={"X-Merchant-Id": mid},
    )
    assert r.status_code == 400
    assert "cannot remove" in r.json()["detail"].lower()


@pytest.mark.asyncio
async def test_get_nearby_merchants_proximity_sorting(auth_client, db_session):
    from app.models.merchant import Merchant

    # Create two merchants at different locations
    # Merchant A is close to user (User is at 12.9716, 77.5946 - Bangalore)
    # Merchant A is at 12.98, 77.60 (~1-2 km away)
    # Merchant B is far away, e.g. at 19.0760, 72.8777 (Mumbai)
    m_close = Merchant(
        slug="close-store",
        legal_name="Close Store",
        display_name="Close",
        referral_code="SIMULA-CLOSE-1",
        is_kyc_completed=True,
        latitude=12.9800,
        longitude=77.6000,
    )
    m_far = Merchant(
        slug="far-store",
        legal_name="Far Store",
        display_name="Far",
        referral_code="SIMULA-FAR-1",
        is_kyc_completed=True,
        latitude=19.0760,
        longitude=72.8777,
    )
    db_session.add_all([m_close, m_far])
    await db_session.commit()

    # Query with user location near Merchant A (Bangalore coords)
    r = await auth_client.get(
        "/api/v1/merchants/public/nearby?lat=12.9716&lon=77.5946"
    )
    assert r.status_code == 200
    body = r.json()
    
    # Verify both are returned but the closer one is first
    assert len(body) >= 2
    slugs = [item["slug"] for item in body]
    # Finding the relative index since other test merchants might be in db
    assert slugs.index("close-store") < slugs.index("far-store")
    
    # Verify distance field is computed
    close_item = next(item for item in body if item["slug"] == "close-store")
    far_item = next(item for item in body if item["slug"] == "far-store")
    assert close_item["distance"] is not None
    assert far_item["distance"] is not None
    assert close_item["distance"] < far_item["distance"]


def test_id_generator_validation_and_parsing():
    from app.utils.id_generator import (
        validate_mpuid,
        validate_mpsuid,
        parse_mpuid,
        parse_mpsuid,
    )

    # Valid MPUID
    mpuid = "SIM-M-MH-000142-M"
    assert validate_mpuid(mpuid) is True
    parsed_m = parse_mpuid(mpuid)
    assert parsed_m["state"] == "MH"
    assert parsed_m["sequence"] == "000142"
    assert parsed_m["city_code"] == "M"

    # Invalid MPUID
    assert validate_mpuid("INVALID-ID") is False
    assert validate_mpuid("m1234567") is False

    # Valid MPSUID
    mpsuid = "SIM-S-000142-01-M"
    assert validate_mpsuid(mpsuid) is True
    parsed_s = parse_mpsuid(mpsuid)
    assert parsed_s["merchant_sequence"] == "000142"
    assert parsed_s["shop_sequence"] == "01"
    assert parsed_s["city_code"] == "M"

    # Invalid MPSUID
    assert validate_mpsuid("S123") is False


@pytest.mark.asyncio
async def test_create_merchant_generates_mpuid_and_mpsuid(auth_client):
    from app.utils.id_generator import validate_mpuid, validate_mpsuid

    # Create primary shop
    r1 = await auth_client.post(
        "/api/v1/merchants/",
        json={
            "legal_name": "Maharashtra Traders Pvt Ltd",
            "display_name": "MH Store 1",
            "state_code": "MH",
            "city_code": "M",
        },
    )
    assert r1.status_code == 201, r1.text
    b1 = r1.json()
    assert validate_mpuid(b1["partner_id"]) is True
    assert validate_mpsuid(b1["shop_id"]) is True
    assert b1["partner_id"].startswith("SIM-M-MH-")
    assert b1["partner_id"].endswith("-M")
    assert b1["shop_id"].startswith("SIM-S-")
    assert b1["shop_id"].endswith("-01-M")

    # Create secondary shop under same owner
    r2 = await auth_client.post(
        "/api/v1/merchants/",
        json={
            "legal_name": "Maharashtra Traders Store 2",
            "display_name": "MH Store 2",
            "state_code": "MH",
            "city_code": "M",
        },
    )
    assert r2.status_code == 201, r2.text
    b2 = r2.json()
    # Should inherit partner_id
    assert b2["partner_id"] == b1["partner_id"]
    # Shop ID sequence should increment to -02-M
    assert b2["shop_id"].endswith("-02-M")


@pytest.mark.asyncio
async def test_validate_id_endpoint(auth_client):
    r = await auth_client.get("/api/v1/merchants/validate-id?id_string=SIM-M-DL-000142-N")
    assert r.status_code == 200
    b = r.json()
    assert b["is_valid"] is True
    assert b["type"] == "MPUID"
    assert b["parsed"]["state"] == "DL"

