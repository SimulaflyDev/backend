from collections.abc import AsyncIterator
from pathlib import Path

from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.core.config import get_settings

settings = get_settings()


class Base(DeclarativeBase):
    pass


def _build_connect_args(url: str) -> dict:
    """Neon/pgbouncer-safe asyncpg settings when talking to a pooled endpoint."""
    if "asyncpg" not in url:
        return {}
    args: dict = {}
    # Disable asyncpg's prepared-statement cache — pgbouncer transaction mode (Neon "-pooler"
    # hosts) cannot preserve server-side prepared statements across connections.
    if "-pooler" in url or "pgbouncer" in url:
        args["statement_cache_size"] = 0
        args["prepared_statement_cache_size"] = 0
    return args


engine_kwargs = {
    "echo": False,
    "connect_args": _build_connect_args(settings.DATABASE_URL),
}

# SQLite doesn't support pool_pre_ping, pool_size, max_overflow
if "sqlite" not in settings.DATABASE_URL.lower():
    engine_kwargs.update({
        "pool_pre_ping": True,
        "pool_size": 10,
        "max_overflow": 20,
    })

engine = create_async_engine(settings.DATABASE_URL, **engine_kwargs)

SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def get_db() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


async def ping_db() -> bool:
    from sqlalchemy import text
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


def _expected_migration_heads() -> set[str]:
    root = Path(__file__).resolve().parents[2]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "alembic"))
    return set(ScriptDirectory.from_config(config).get_heads())


async def schema_is_current() -> bool:
    """Return false when the database revision is missing or behind the code."""
    try:
        expected = _expected_migration_heads()
        async with engine.connect() as connection:
            current = await connection.run_sync(
                lambda sync_connection: set(
                    MigrationContext.configure(sync_connection).get_current_heads()
                )
            )
        return bool(expected) and current == expected
    except Exception:
        return False
