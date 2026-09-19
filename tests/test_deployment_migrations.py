from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_vm_deployment_cannot_start_when_migrations_fail():
    script = (ROOT / "deploy.sh").read_text(encoding="utf-8")
    migration = script[script.index("# Step 8:"):script.index("# Step 9:")]
    assert 'alembic" upgrade head' in migration
    assert migration.count("exit 1") >= 2
    assert "Skipping database migrations" not in migration


def test_all_supported_start_commands_migrate_before_serving():
    procfile = (ROOT / "Procfile").read_text(encoding="utf-8").strip()
    assert procfile.startswith("web: alembic upgrade head && ")


def test_maintenance_scripts_do_not_embed_database_credentials():
    for name in ("run_migration.py", "update_db.py", "query_db.py"):
        source = (ROOT / name).read_text(encoding="utf-8")
        assert "postgresql+asyncpg://" not in source
