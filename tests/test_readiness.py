import pytest

import app.main as main_module


@pytest.mark.asyncio
async def test_readyz_requires_database_and_current_schema(client, monkeypatch):
    async def healthy():
        return True

    async def unhealthy():
        return False

    monkeypatch.setattr(main_module, "ping_db", healthy)
    monkeypatch.setattr(main_module, "schema_is_current", healthy)
    response = await client.get("/readyz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "db": True, "schema": True}

    monkeypatch.setattr(main_module, "schema_is_current", unhealthy)
    response = await client.get("/readyz")
    assert response.status_code == 503
    assert response.json() == {"status": "degraded", "db": True, "schema": False}

    monkeypatch.setattr(main_module, "ping_db", unhealthy)
    response = await client.get("/readyz")
    assert response.status_code == 503
    assert response.json() == {"status": "degraded", "db": False, "schema": False}


@pytest.mark.asyncio
async def test_unversioned_test_database_is_not_reported_current(db_engine, monkeypatch):
    import app.core.database as database

    monkeypatch.setattr(database, "engine", db_engine)
    assert await database.schema_is_current() is False


@pytest.mark.asyncio
async def test_production_startup_refuses_an_outdated_schema(monkeypatch):
    async def outdated():
        return False

    monkeypatch.setattr(main_module.settings, "ENV", "production")
    monkeypatch.setattr(main_module, "schema_is_current", outdated)
    with pytest.raises(RuntimeError, match="migrations are not at"):
        async with main_module.lifespan(main_module.app):
            pass
