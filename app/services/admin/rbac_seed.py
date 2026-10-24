"""Idempotent seed of the predefined RBAC roles and permission catalog.

Mirrors the ``app/services/style_seed.py::seed_if_empty`` bootstrap pattern:

  * ``seed_rbac_if_empty(db)`` — called from the FastAPI lifespan (wired in a
    later task). No-ops if the ``admin_roles`` table already has rows. Used for
    first-run / fresh-deploy bootstrap.

  * Module-as-script: ``python -m app.services.admin.rbac_seed`` — runs the
    seed against the configured DATABASE_URL. Pass ``--sync`` to force a
    reconciling upsert (insert any missing permissions/roles and attach any
    missing role→permission grants) even when the table is already populated.

The underlying insert routine is itself idempotent: it looks up every
permission by ``key`` and every role by ``name`` before inserting, and only
attaches role→permission grants that are not already present. Running the seed
twice therefore never creates duplicates (Requirements 2.1, 2.2).

RBAC model:
  * ``Permission`` — a granular capability string such as ``wallets.adjust``.
  * ``Role`` — a named bundle of permissions.
  * Super Admin holds the wildcard permission ``*`` which satisfies any check
    (see ``app/services/admin/rbac_service.py``).
"""
from __future__ import annotations

import argparse
import asyncio
import logging

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.database import SessionLocal
from app.models.admin import Permission, Role
from app.services.admin.rbac_service import WILDCARD_PERMISSION

log = logging.getLogger(__name__)


# --- Permission catalog -----------------------------------------------------
# Every permission referenced by the design's Endpoint Inventory (the
# "Permission" column), keyed by its string with a human-readable description.
# The wildcard ``*`` (Super Admin) is seeded separately so it is never handed
# to a non-Super-Admin role by mistake.
PERMISSION_CATALOG: dict[str, str] = {
    "content.read": "View app home carousel content.",
    "content.manage": "Edit and publish app home carousel content and images.",
    "dashboard.read": "View the executive dashboard KPIs and series.",
    "alerts.read": "View operational alert counters and underlying items.",
    "alerts.resolve": "Resolve operational alert items.",
    "users.read": "View consumer users (directory and detail).",
    "users.suspend": "Suspend or reactivate consumer users.",
    "merchants.read": "View merchants (directory and detail).",
    "merchants.suspend": "Suspend or activate merchants.",
    "stores.read": "View stores (directory and detail).",
    "stores.manage": "Change store status.",
    "products.read": "View merchant products (directory and detail).",
    "products.manage": "Change product visibility/status.",
    "bookings.read": "View bookings, disputes, and fulfillment queues.",
    "bookings.manage": "Resolve disputes and update fulfillment.",
    "finance.read": "View the financial dashboard and transaction breakdowns.",
    "pricing.read": "View pricing rules.",
    "pricing.manage": "Create and update pricing rules.",
    "wallets.read": "View wallets and wallet transactions.",
    "wallets.adjust": "Credit or debit wallet balances.",
    "redeem.read": "View redeem/promo codes.",
    "redeem.manage": "Create and deactivate redeem/promo codes.",
    "invoices.read": "View invoices and invoice detail.",
    "invoices.manage": "Mark invoices paid and manage their lifecycle.",
    "support.read": "View support tickets and message history.",
    "support.respond": "Append support messages and change ticket status.",
    "analytics.read": "View analytics (user, merchant, wallet, AI usage).",
    "audit.read": "View the immutable admin audit log.",
    "accounts.read": "View admin accounts.",
    "accounts.manage": "Invite, deactivate, reactivate, and assign roles to admin accounts.",
    "roles.read": "View roles and their permissions.",
    "roles.manage": "Create roles and modify role permissions.",
    "system.read": "View system health, counters, and webhook deliveries.",
    "system.manage": "Redeliver webhooks and perform system actions.",
}


# --- Predefined roles -------------------------------------------------------
# Super Admin is granted the wildcard permission. The other three roles get a
# sensible, least-privilege bundle aligned with the design's permission column.

_FINANCE_PERMISSIONS = [
    "finance.read",
    "pricing.read",
    "pricing.manage",
    "wallets.read",
    "wallets.adjust",
    "redeem.read",
    "redeem.manage",
    "invoices.read",
    "invoices.manage",
]

_SUPPORT_PERMISSIONS = [
    "support.read",
    "support.respond",
    "users.read",
]

_ORG_MANAGER_PERMISSIONS = [
    "content.read",
    "content.manage",
    # Broad read across the operational surface...
    "dashboard.read",
    "alerts.read",
    "users.read",
    "merchants.read",
    "stores.read",
    "products.read",
    "bookings.read",
    "finance.read",
    "analytics.read",
    "support.read",
    "audit.read",
    "accounts.read",
    "roles.read",
    "system.read",
    # ...plus day-to-day operational actions (no account/role/system mgmt).
    "alerts.resolve",
    "users.suspend",
    "merchants.suspend",
    "stores.manage",
    "products.manage",
    "bookings.manage",
    "support.respond",
]


class _RoleSpec:
    __slots__ = ("name", "description", "permission_keys")

    def __init__(self, name: str, description: str, permission_keys: list[str]):
        self.name = name
        self.description = description
        self.permission_keys = permission_keys


PREDEFINED_ROLES: list[_RoleSpec] = [
    _RoleSpec(
        "Super Admin",
        "Full, unrestricted access via the wildcard permission.",
        [WILDCARD_PERMISSION],
    ),
    _RoleSpec(
        "Org Manager",
        "Broad read access plus day-to-day operational actions.",
        _ORG_MANAGER_PERMISSIONS,
    ),
    _RoleSpec(
        "Finance",
        "Finance, pricing, wallets, redeem codes, and invoices.",
        _FINANCE_PERMISSIONS,
    ),
    _RoleSpec(
        "Support",
        "Support tickets (read + respond) and user lookups.",
        _SUPPORT_PERMISSIONS,
    ),
]


async def _roles_count(db: AsyncSession) -> int:
    res = await db.execute(select(func.count()).select_from(Role))
    return int(res.scalar() or 0)


async def _ensure_permissions(db: AsyncSession) -> dict[str, Permission]:
    """Insert any missing permissions (catalog + wildcard); return key->row."""
    keys = set(PERMISSION_CATALOG) | {WILDCARD_PERMISSION}

    existing = {
        p.key: p
        for p in (
            await db.execute(select(Permission).where(Permission.key.in_(keys)))
        ).scalars()
    }

    for key in keys:
        if key in existing:
            continue
        if key == WILDCARD_PERMISSION:
            description = "Wildcard — satisfies every permission check (Super Admin)."
        else:
            description = PERMISSION_CATALOG[key]
        perm = Permission(key=key, description=description)
        db.add(perm)
        existing[key] = perm

    await db.flush()
    return existing


async def _ensure_roles(
    db: AsyncSession,
    permissions: dict[str, Permission],
) -> int:
    """Insert any missing predefined roles and attach missing grants.

    Returns the number of roles created (existing roles are reconciled in place
    but not counted as created)."""
    created = 0
    for spec in PREDEFINED_ROLES:
        role = (
            await db.execute(
                select(Role)
                .where(Role.name == spec.name)
                .options(selectinload(Role.permissions))
            )
        ).scalar_one_or_none()

        if role is None:
            role = Role(
                name=spec.name,
                description=spec.description,
                is_predefined=True,
            )
            db.add(role)
            created += 1

        # Attach any missing grants (idempotent — never duplicates a pair).
        current_keys = {p.key for p in role.permissions}
        for key in spec.permission_keys:
            if key not in current_keys:
                role.permissions.append(permissions[key])
                current_keys.add(key)

    await db.flush()
    return created


async def _seed(db: AsyncSession) -> int:
    """Idempotent seed core. Returns the number of roles created."""
    permissions = await _ensure_permissions(db)
    created = await _ensure_roles(db, permissions)
    await db.commit()
    log.info(
        "rbac seed: permissions=%d roles_created=%d",
        len(permissions),
        created,
    )
    return created


async def seed_rbac_if_empty(db: AsyncSession) -> int:
    """Seed predefined roles + permission catalog only if no roles exist.

    Mirrors ``style_seed.seed_if_empty``: a no-op fast path when the
    ``admin_roles`` table is already populated. Returns the number of roles
    created (0 when skipped). The seed itself is idempotent, so it is safe to
    invoke even if the fast-path check is removed.
    """
    count = await _roles_count(db)
    if count > 0:
        log.info("admin_roles table has %d rows — skipping initial RBAC seed", count)
        return 0
    return await _seed(db)


async def _main(args: argparse.Namespace) -> int:
    async with SessionLocal() as db:
        if args.sync:
            created = await _seed(db)
            print(f"Done — reconciled RBAC catalog, created {created} role(s).")
        else:
            created = await seed_rbac_if_empty(db)
            print(f"Done — created {created} role(s).")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Seed the predefined admin roles and permission catalog.",
    )
    parser.add_argument(
        "--sync",
        action="store_true",
        help="Reconcile (insert missing permissions/roles/grants) even if populated.",
    )
    cli_args = parser.parse_args()
    raise SystemExit(asyncio.run(_main(cli_args)))
