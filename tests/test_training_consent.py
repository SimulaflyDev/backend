import pytest
from sqlalchemy import select

from app.models.user import User
from app.services.google_auth import GoogleIdentity


@pytest.mark.asyncio
@pytest.mark.parametrize("choice", [None, True, False])
async def test_registration_consent_default_and_explicit_optout(client, choice):
    body = {"email": "consent@example.com", "password": "password123"}
    if choice is not None:
        body["model_improvement_consent"] = choice
    response = await client.post("/api/v1/auth/register", json=body)
    assert response.status_code == 201, response.text
    assert response.json()["model_improvement_consent"] is (True if choice is None else choice)


@pytest.mark.asyncio
async def test_google_default_does_not_override_existing_optout(client, db_session, monkeypatch):
    from app.routers import auth
    identity = GoogleIdentity(sub="consent-google", email="google-consent@example.com", email_verified=True, full_name="Test", picture=None)
    monkeypatch.setattr(auth, "verify_id_token", lambda _: identity)
    first = await client.post("/api/v1/auth/google", json={"id_token": "fake-test-token", "model_improvement_consent": False})
    assert first.status_code == 200, first.text
    user = (await db_session.execute(select(User).where(User.email == identity.email))).scalar_one()
    assert user.model_improvement_consent is False
    second = await client.post("/api/v1/auth/google", json={"id_token": "fake-test-token", "model_improvement_consent": True})
    assert second.status_code == 200
    await db_session.refresh(user)
    assert user.model_improvement_consent is False


@pytest.mark.asyncio
async def test_optout_persists_in_account(auth_client, db_session, test_user):
    response = await auth_client.patch("/api/v1/users/me", json={"model_improvement_consent": False})
    assert response.status_code == 200
    await db_session.refresh(test_user)
    assert test_user.model_improvement_consent is False
    me = await auth_client.get("/api/v1/users/me")
    assert me.json()["model_improvement_consent"] is False
