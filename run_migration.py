"""Apply this checkout's Alembic migrations using DATABASE_URL from .env."""
from pathlib import Path

from alembic import command
from alembic.config import Config


def main() -> None:
    root = Path(__file__).resolve().parent
    config = Config(str(root / "alembic.ini"))
    command.upgrade(config, "head")
    print("Database migrations are at head.")


if __name__ == "__main__":
    main()
