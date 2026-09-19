from unittest.mock import Mock

import pytest


@pytest.mark.asyncio
async def test_register_and_login(client):
    r = await client.post(
        "/api/v1/auth/register",
        json={"email": "alice@example.com", "password": "password123", "full_name": "Alice"},
    )
    assert r.status_code == 201, r.text
    assert r.json()["email"] == "alice@example.com"

    r = await client.post(
        "/api/v1/auth/login",
        json={"email": "alice@example.com", "password": "password123"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["access_token"] and body["refresh_token"]

    refresh = body["refresh_token"]
    r = await client.post("/api/v1/auth/refresh", json={"refresh_token": refresh})
    assert r.status_code == 200
    new_body = r.json()
    assert new_body["access_token"] and new_body["refresh_token"]


@pytest.mark.asyncio
async def test_register_duplicate_email_conflict(client):
    payload = {"email": "dup@example.com", "password": "password123"}
    r1 = await client.post("/api/v1/auth/register", json=payload)
    assert r1.status_code == 201
    r2 = await client.post("/api/v1/auth/register", json=payload)
    assert r2.status_code == 409


@pytest.mark.asyncio
async def test_registration_does_not_send_a_second_implicit_otp(client, monkeypatch):
    from app.routers import auth

    send_email = Mock(return_value=True)
    monkeypatch.setattr(auth, "send_otp_email", send_email)

    response = await client.post(
        "/api/v1/auth/register",
        json={"email": "single-otp@example.com", "password": "password123"},
    )

    assert response.status_code == 201, response.text
    send_email.assert_not_called()


@pytest.mark.asyncio
async def test_repeated_email_otp_requests_send_only_one_email(auth_client, monkeypatch):
    from app.routers import auth

    send_email = Mock(return_value=True)
    monkeypatch.setattr(auth, "send_otp_email", send_email)

    first = await auth_client.post("/api/v1/auth/send-otp")
    second = await auth_client.post("/api/v1/auth/send-otp")

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert "already sent" in second.json()["message"]
    send_email.assert_called_once()


@pytest.mark.asyncio
async def test_login_wrong_password_returns_401(client):
    await client.post(
        "/api/v1/auth/register",
        json={"email": "bob@example.com", "password": "password123"},
    )
    r = await client.post(
        "/api/v1/auth/login",
        json={"email": "bob@example.com", "password": "wrongpassword"},
    )
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_me_requires_auth(client):
    r = await client.get("/api/v1/users/me")
    assert r.status_code == 403 or r.status_code == 401


@pytest.mark.asyncio
async def test_me_with_valid_token(auth_client, test_user):
    r = await auth_client.get("/api/v1/users/me")
    assert r.status_code == 200
    assert r.json()["id"] == str(test_user.id)


@pytest.mark.asyncio
async def test_update_me_settings(auth_client, test_user):
    # Initial states
    assert test_user.push_notifications is True
    assert test_user.marketing_consent is True
    assert test_user.model_improvement_consent is True
    assert test_user.buyer_signal_sharing is True

    # Update preferences
    r = await auth_client.patch(
        "/api/v1/users/me",
        json={
            "push_notifications": False,
            "marketing_consent": False,
            "model_improvement_consent": False,
            "buyer_signal_sharing": False,
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["push_notifications"] is False
    assert body["marketing_consent"] is False
    assert body["model_improvement_consent"] is False
    assert body["buyer_signal_sharing"] is False


@pytest.mark.asyncio
async def test_delete_me(auth_client, test_user):
    r = await auth_client.delete("/api/v1/users/me")
    assert r.status_code == 204
    r = await auth_client.get("/api/v1/users/me")
    assert r.status_code in (401, 403)


