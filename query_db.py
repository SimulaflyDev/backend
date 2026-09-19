"""Print non-PII database/order diagnostics using DATABASE_URL from .env."""
import asyncio

from sqlalchemy import text

from app.core.database import engine


async def main() -> None:
    async with engine.connect() as connection:
        counts = {}
        for table in ("buyer_leads", "orders"):
            counts[table] = (
                await connection.execute(text(f"SELECT count(*) FROM {table}"))
            ).scalar_one()
        revisions = list(
            (await connection.execute(text("SELECT version_num FROM alembic_version")))
            .scalars()
            .all()
        )
        print({"row_counts": counts, "migration_revisions": revisions})
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
