"""Apply this checkout's Alembic migrations using DATABASE_URL from .env."""
from pathlib import Path
import os

from alembic import command
from alembic.config import Config


def main() -> None:
    root = Path(__file__).resolve().parent
    # Resolve both Alembic sources and .env from this checkout even when the
    # operator starts the command in a different working directory.
    os.chdir(root)
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "alembic"))
    command.upgrade(config, "head")
    print("Database migrations are at head.")


if __name__ == "__main__":
    main()
