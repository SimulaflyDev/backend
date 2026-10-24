"""Read-only production checks; never logs secrets or changes the database.

Run from the backend environment: python -m scripts.release_preflight
"""
import asyncio
import json
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import get_settings


async def main() -> int:
    settings = get_settings()
    url = make_url(settings.DATABASE_URL)
    root = Path(__file__).resolve().parents[1]
    config = Config(str(root / 'alembic.ini'))
    config.set_main_option('script_location', str(root / 'alembic'))
    scripts = ScriptDirectory.from_config(config)
    heads = scripts.get_heads()
    issues = []
    if not settings.is_production:
        issues.append('ENV must be production for the release server')
    if '*' in settings.ALLOWED_ORIGINS:
        issues.append('Replace wildcard CORS origins with deployed frontend origins')
    if not settings.ai_configured:
        issues.append('AI credentials/endpoint are not configured')
    if not settings.AZURE_IMAGE_EDIT_DEPLOYMENT:
        issues.append('Image edit deployment is not configured')
    if len(heads) != 1:
        issues.append('Migration graph does not have exactly one head')
    report = {
        'environment': settings.ENV, 'database_host': url.host, 'database_name': url.database,
        'code_heads': heads, 'configuration_issues': issues,
    }
    engine = create_async_engine(settings.DATABASE_URL, echo=False, connect_args={'timeout': 15})
    try:
        async with asyncio.timeout(20):
            async with engine.connect() as connection:
                revisions = list((await connection.execute(text('SELECT version_num FROM alembic_version'))).scalars())
                report['database_revisions'] = revisions
                report['migrations_current'] = set(revisions) == set(heads)
                report['pending_revisions'] = [rev.revision for rev in scripts.iterate_revisions(heads, revisions)]
    except Exception as error:
        report['database_check'] = 'failed'
        report['error_type'] = type(error).__name__
        # Database exception messages can contain credentials; do not print them.
    finally:
        await engine.dispose()
    print(json.dumps(report, indent=2))
    return 0 if report.get('migrations_current') and not issues else 1


if __name__ == '__main__':
    raise SystemExit(asyncio.run(main()))
