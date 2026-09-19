from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from starlette.middleware.base import BaseHTTPMiddleware

from app.core.config import get_settings
from app.core.database import SessionLocal, engine, ping_db, schema_is_current
from app.core.logging import configure_logging, get_logger
from app.core.rate_limit import limiter
from app.routers.admin import router as admin_router
from app.routers import (
    analytics,
    auth,
    buyer_intelligence,
    buyer_leads,
    cart,
    chat,
    contacts,
    coupons,
    events,
    leads,
    merchant_products,
    merchant_onboarding,
    merchant_verification,
    merchants,
    notifications,
    products,
    saved,
    sessions,
    styles,
    support,
    upload,
    users,
    visualization,
    wallet,
    webhooks,
)

settings = get_settings()
configure_logging()
log = get_logger("app.main")


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("startup", env=settings.ENV, ai_configured=settings.ai_configured)

    # Do not serve production traffic with ORM models newer than the database.
    # The deployment normally migrates first; this catches manual starts or a
    # misconfigured process manager before they become endpoint-specific 500s.
    if settings.is_production and not await schema_is_current():
        log.error("startup_blocked_database_schema_outdated")
        raise RuntimeError("Database migrations are not at the application head")

    # First-run bootstrap of the editorial style catalog. No-op if the table
    # already has rows. Failures here are logged but don't block startup —
    # the app is usable without styles, just shows an empty discovery feed.
    try:
        from app.services.style_seed import seed_if_empty
        async with SessionLocal() as db:
            inserted = await seed_if_empty(db)
        if inserted:
            log.info("style_seed_inserted", count=inserted)
    except Exception as e:  # noqa: BLE001
        log.warning("style_seed_skipped", error=str(e))

    # First-run bootstrap of admin RBAC (predefined roles + permission catalog)
    # and a Super Admin account from env vars, so admin login is usable on a
    # fresh deploy. Both are idempotent. Failures are logged but never block
    # startup — the rest of the app remains usable.
    try:
        from app.services.admin.admin_bootstrap import bootstrap_super_admin
        from app.services.admin.rbac_seed import seed_rbac_if_empty
        async with SessionLocal() as db:
            await seed_rbac_if_empty(db)
            await bootstrap_super_admin(db)
    except Exception as e:  # noqa: BLE001
        log.warning("admin_bootstrap_skipped", error=str(e))

    # Phase 4: periodic pause-if-depleted sweep
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from app.services.billing import BillingService

    async def _pause_sweep():
        async with SessionLocal() as db:
            svc = BillingService(db)
            paused = await svc.pause_if_depleted_all()
            if paused > 0:
                log.info("pause_sweep_paused_merchants", count=paused)

    # Phase 5 (admin panel): periodic derived-state sweeps. Both are
    # side-effect-light and non-destructive — they evaluate the same derived
    # predicates the admin API exposes (overdue invoices = unpaid AND past due;
    # SLA breach = unresolved AND past sla_due_at) and emit counters for
    # observability without mutating any rows.
    from datetime import datetime, timezone

    from sqlalchemy import func, select

    from app.models.invoice import Invoice, InvoiceStatus
    from app.models.support import SupportTicket, SupportTicketStatus

    async def _overdue_invoice_sweep():
        now = datetime.now(timezone.utc)
        async with SessionLocal() as db:
            stmt = select(func.count()).select_from(Invoice).where(
                Invoice.deleted_at.is_(None),
                Invoice.status == InvoiceStatus.UNPAID.value,
                Invoice.due_date < now,
            )
            overdue = (await db.execute(stmt)).scalar_one()
        if overdue > 0:
            log.info("overdue_invoice_sweep", count=overdue)

    async def _sla_breach_sweep():
        now = datetime.now(timezone.utc)
        async with SessionLocal() as db:
            stmt = select(func.count()).select_from(SupportTicket).where(
                SupportTicket.deleted_at.is_(None),
                SupportTicket.status != SupportTicketStatus.RESOLVED.value,
                SupportTicket.sla_due_at.is_not(None),
                SupportTicket.sla_due_at < now,
            )
            breached = (await db.execute(stmt)).scalar_one()
        if breached > 0:
            log.info("sla_breach_sweep", count=breached)

    scheduler = AsyncIOScheduler()
    scheduler.add_job(_pause_sweep, "interval", minutes=5, id="pause_sweep")
    scheduler.add_job(
        _overdue_invoice_sweep, "interval", minutes=5, id="overdue_invoice_sweep"
    )
    scheduler.add_job(_sla_breach_sweep, "interval", minutes=5, id="sla_breach_sweep")
    scheduler.start()
    app.state.scheduler = scheduler

    yield
    scheduler.shutdown(wait=False)
    await engine.dispose()
    log.info("shutdown")


def create_app() -> FastAPI:
    docs_url = None if settings.is_production else "/docs"
    redoc_url = None if settings.is_production else "/redoc"

    app = FastAPI(
        title="simulafly Backend",
        version="1.0.0",
        docs_url=docs_url,
        redoc_url=redoc_url,
        lifespan=lifespan,
    )

    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.ALLOWED_ORIGINS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.add_middleware(RequestSizeLimitMiddleware, max_bytes=settings.MAX_REQUEST_BYTES)

    api_prefix = "/api/v1"
    app.include_router(analytics.router, prefix=api_prefix)
    app.include_router(buyer_intelligence.router, prefix=api_prefix)
    app.include_router(contacts.router, prefix=api_prefix)
    app.include_router(leads.router, prefix=api_prefix)
    app.include_router(buyer_leads.router, prefix=api_prefix)
    app.include_router(auth.router, prefix=api_prefix)
    app.include_router(merchants.router, prefix=api_prefix)
    app.include_router(merchant_onboarding.router, prefix=api_prefix)
    app.include_router(merchant_verification.router, prefix=api_prefix)
    app.include_router(merchant_products.router, prefix=api_prefix)
    app.include_router(support.router, prefix=api_prefix)
    app.include_router(wallet.router, prefix=api_prefix)
    app.include_router(webhooks.router, prefix=api_prefix)
    app.include_router(users.router, prefix=api_prefix)
    app.include_router(sessions.router, prefix=api_prefix)
    app.include_router(chat.router, prefix=api_prefix)
    app.include_router(events.router, prefix=api_prefix)
    app.include_router(visualization.router, prefix=api_prefix)
    app.include_router(cart.router, prefix=api_prefix)
    app.include_router(saved.router, prefix=api_prefix)
    app.include_router(notifications.router, prefix=api_prefix)
    app.include_router(products.router, prefix=api_prefix)
    app.include_router(styles.router, prefix=api_prefix)
    app.include_router(upload.router, prefix=api_prefix)
    app.include_router(coupons.router, prefix=api_prefix)
    app.include_router(admin_router, prefix=api_prefix)

    # Static style images — served at /static/styles/imageN.jpg.
    # Source: `data/style_templates/images/` (compressed from PNG → JPEG).
    # The `styles` router builds absolute URLs pointing here.
    styles_dir = (
        Path(__file__).resolve().parent.parent
        / "data"
        / "style_templates"
        / "images"
    )
    if styles_dir.is_dir():
        app.mount(
            "/static/styles",
            StaticFiles(directory=str(styles_dir)),
            name="style-images",
        )

    # Static testbed UI (dev helper — access at /testbed/)
    testbed_dir = Path(__file__).resolve().parent.parent / "testbed"
    if testbed_dir.is_dir():
        app.mount("/testbed", StaticFiles(directory=str(testbed_dir), html=True), name="testbed")

    @app.get("/", include_in_schema=False)
    async def root():
        return RedirectResponse(url="/testbed/" if testbed_dir.is_dir() else "/docs")

    @app.get("/healthz", tags=["health"])
    async def healthz():
        return {"status": "ok"}

    @app.get("/readyz", tags=["health"])
    async def readyz():
        db_ok = await ping_db()
        schema_ok = await schema_is_current() if db_ok else False
        ready = db_ok and schema_ok
        return JSONResponse(
            {
                "status": "ok" if ready else "degraded",
                "db": db_ok,
                "schema": schema_ok,
            },
            status_code=200 if ready else 503,
        )

    return app


class RequestSizeLimitMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, max_bytes: int) -> None:
        super().__init__(app)
        self.max_bytes = max_bytes

    async def dispatch(self, request: Request, call_next):
        cl = request.headers.get("content-length")
        if cl and cl.isdigit() and int(cl) > self.max_bytes:
            return JSONResponse(
                {"detail": f"request body too large (max {self.max_bytes} bytes)"},
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            )
        return await call_next(request)


app = create_app()
