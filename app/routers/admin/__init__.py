"""Admin API routers package.

Individual admin sub-routers live in this package (auth, rbac, accounts, etc.).
This module assembles an aggregating ``router`` that collects every admin
sub-router under a single mount. Each sub-router already declares
``prefix="/admin"``, so the aggregating router adds no prefix of its own —
final paths stay ``/admin/...`` and ``main.py`` mounts this package under
``/api/v1`` → ``/api/v1/admin/...``.
"""

from fastapi import APIRouter

from app.routers.admin import (
    accounts,
    alerts,
    analytics,
    audit,
    auth,
    bookings,
    bulk,
    dashboard,
    export,
    finance,
    home_content,
    invoices,
    merchants,
    pricing,
    products,
    rbac,
    redeem_codes,
    stores,
    support,
    system,
    users,
    wallets,
)

router = APIRouter()

router.include_router(auth.router)
router.include_router(home_content.router)
router.include_router(accounts.router)
router.include_router(rbac.router)
router.include_router(users.router)
router.include_router(merchants.router)
router.include_router(stores.router)
router.include_router(products.router)
router.include_router(dashboard.router)
router.include_router(alerts.router)
router.include_router(bookings.router)
router.include_router(finance.router)
router.include_router(pricing.router)
router.include_router(wallets.router)
router.include_router(redeem_codes.router)
router.include_router(invoices.router)
router.include_router(support.router)
router.include_router(analytics.router)
router.include_router(audit.router)
router.include_router(system.router)
router.include_router(export.router)
router.include_router(bulk.router)
