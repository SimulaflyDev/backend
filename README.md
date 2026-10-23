# simulafly Backend

Production-grade FastAPI backend for simulafly — a mobile-first AI interior design PWA.
Scan → Consult → RAG Suggest → Visualize → Cart → Buy, over a real 3,110-row Amazon
India furniture catalog, powered by Azure AI Foundry (GPT-4o, gpt-image-1 edit,
text-embedding-3-small).

## Stack

- **FastAPI 0.115** (async) + **uvicorn**
- **PostgreSQL 16 + pgvector** (IVFFlat, 1536-dim embeddings)
- **SQLAlchemy 2.0 async** + asyncpg + **Alembic**
- **Azure AI Foundry** — gpt-4o (chat + vision), text-embedding-3-small, **gpt-image-1** (edit; primary), DALL-E 3 (fallback)
- **JWT** (python-jose) + **bcrypt** (passlib)
- **SlowAPI** in-memory rate limiting (single-process; swap storage for multi-worker)
- **structlog** (JSON in prod)

## Layout

See `app/` for the layered source. Key modules:

| Module | Purpose |
|---|---|
| `app/main.py` | App factory, middleware, health routes |
| `app/core/config.py` | pydantic-settings, env vars |
| `app/services/azure_ai_client.py` | Azure wrapper + demo fallback |
| `app/services/rag_service.py` | 5-step RAG pipeline |
| `app/services/image_service.py` | bytea persist/fetch (single seam for future blob migration) |
| `app/services/product_ingestion.py` | CSV → embeddings → pgvector |
| `app/routers/chat.py` | Core AI endpoint; parses `PRODUCTS_JSON` / `PREVIEW_REQUEST` |
| `app/routers/visualization.py` | `gpt-image-1` edit composite |
| `alembic/versions/0001_initial_schema.py` | All tables + pgvector extension |

## Prerequisites

You need a local PostgreSQL 16 with the `pgvector` extension. A few easy options:

- **Windows installer**: install [PostgreSQL 16](https://www.postgresql.org/download/windows/), then install pgvector via [pgvector for Windows](https://github.com/pgvector/pgvector#installation-notes).
- **Managed cloud**: use a free-tier Postgres with pgvector built-in (e.g., Neon, Supabase). Point `DATABASE_URL` at it.

Once Postgres is running, create the database and user (from `psql` as superuser):

```sql
CREATE USER simulafly WITH PASSWORD 'simulafly';
CREATE DATABASE simulafly OWNER simulafly;
```

## Setup

```bash
cp .env.example .env                     # fill SECRET_KEY (openssl rand -hex 32) and Azure keys

python -m venv .venv
source .venv/Scripts/activate            # Windows (bash). Linux/Mac: source .venv/bin/activate
pip install -r requirements.txt

alembic upgrade head
python -m app.services.product_ingestion # needs data/amazon_products.csv + Azure embed key

py
```
uvicorn app.main:app --host 0.0.0.0 --port 8000

For production, run with multiple workers:

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 4
```

> **Note**: rate limiting uses in-memory storage (per-process). With `--workers > 1`,
> limits become per-worker. If you later need accurate shared limits, swap
> `app/core/rate_limit.py` to a shared backend.

## Environment variables

### Merchant verification and password-reset email

Configure `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, and `SMTP_PASSWORD` in the
**running API server's environment** (or its `.env`), then restart the API.
Mail credentials are no longer embedded in Python source. Existing deployments
must set these values before deploying this update. For Gmail / Google Workspace,
use a valid app password belonging to the `SMTP_USER` mailbox. Port 587 uses
STARTTLS; port 465 uses implicit TLS. Both verify the server certificate.

Run `python -m scripts.check_email` from the backend directory on that server to
test connection and login **without sending email**. A disconnect at login means
mailbox access or the mail connection still needs attention; a local test does
not establish connectivity from the deployed host. API logs distinguish
connection/authentication failures from delivery failures without logging codes
or passwords. Failed sends remove the reset code so the merchant can retry.

The merchant frontend uses `NEXT_PUBLIC_API_BASE_URL`; if this points to the live
API, editing local backend files alone does not update the email sender in use.

See `.env.example` for the full list. Notable entries:

| Var | Purpose |
|---|---|
| `SECRET_KEY` | ≥32-char JWT signing key |
| `DATABASE_URL` | `postgresql+asyncpg://user:pass@host:5432/db` |
| `AZURE_AI_FOUNDRY_ENDPOINT` / `_API_KEY` | Credentials |
| `AZURE_CHAT_DEPLOYMENT` | Default: `gpt-4o` |
| `AZURE_EMBEDDING_DEPLOYMENT` | Default: `text-embedding-3-small` |
| **`AZURE_IMAGE_EDIT_DEPLOYMENT`** | **`gpt-image-1` deployment — primary for `/visualize/`** |
| `AZURE_IMAGE_GEN_DEPLOYMENT` | DALL-E 3 deployment — fallback only |
| `ALLOWED_ORIGINS` | Comma-separated CORS origins |
| `RATE_LIMIT_PER_MINUTE` | Default: 60 |
| `CHAT_RATE_LIMIT_PER_MINUTE` | Default: 20 |
| `IMAGE_GEN_RATE_LIMIT_PER_HOUR` | Default: 10 |

**Visualization behavior**:
- `AZURE_IMAGE_EDIT_DEPLOYMENT` set → true scene-preserving composite (primary path)
- Only `AZURE_IMAGE_GEN_DEPLOYMENT` set → DALL-E 3 fallback, logs warning
- Neither set → placeholder PNG (demo mode)

## API (`/api/v1` prefix)

| Group | Endpoints |
|---|---|
| Auth | `POST /auth/register`, `POST /auth/login`, `POST /auth/refresh` |
| Users | `GET|PATCH /users/me` |
| Sessions | `POST /sessions/`, `GET /sessions/`, `GET|PATCH|DELETE /sessions/{id}` |
| Chat | `POST /chat/analyze`, `POST /chat/`, `GET /chat/{session_id}/messages` |
| Visualize | `POST /visualize/` |
| Cart | `GET|POST /cart/`, `PATCH|DELETE /cart/{item_id}`, `DELETE /cart/` |
| Saved | `GET|POST /saved/`, `PATCH|DELETE /saved/{item_id}`, `DELETE /saved/by-product/{product_id}` |
| Notifications | `GET /notifications/?unread_only=`, `POST /notifications/{id}/read`, `POST /notifications/read-all`, `DELETE /notifications/{id}` |
| Styles | `GET /styles/`, `GET /styles/{slug}` (public reads); `POST /styles/`, `PATCH /styles/{slug}`, `DELETE /styles/{slug}?hard=` (admin — `X-Admin-Key` header). Images served at `/static/styles/imageNN.jpg` |
| Products | `GET /products/search?q=`, `GET /products/?category=&max_price=&limit=&offset=`, `GET /products/{id}` |
| Upload | `POST /upload/room-image`, `GET /upload/room-image/{id}` |
| Health | `GET /healthz`, `GET /readyz` |

Interactive docs at `/docs` (development only).

## Tests

```bash
pytest tests/ -v
```

Tests run against aiosqlite in-memory; Azure AI calls are served from deterministic
mock mode (no credentials required). For real Azure integration tests, set
`RUN_INTEGRATION=1` and valid Azure env vars.

## Style catalog

The editorial design-style templates (Mid-Century Modern, Boho Chic, …) live
in the `styles` table. Migration `0004_styles` creates the table; the
FastAPI lifespan auto-seeds it from
`data/style_templates/templates.json` on first run (no-op if rows exist).

To add new styles:

```bash
# 1. Drop the new image into data/style_templates/images/<file>.jpg
# 2. Hit the admin endpoint:
curl -X POST https://your.host/api/v1/styles/ \
  -H "X-Admin-Key: $ADMIN_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"name":"My New Style","category":"Living Room","vibe":"…","description":"…","image_filename":"my_style.jpg","trending":false}'
```

Or batch from JSON: edit `templates.json`, add new entries with unique
`id`s, then re-run:

```bash
python -m app.services.style_seed --upsert   # idempotent: insert + update
python -m app.services.style_seed --reset    # WIPE the table first (destructive)
```

`ADMIN_API_KEY` (env var, generate with `openssl rand -hex 32`) gates
the write endpoints. Empty / unset → admin endpoints return 503.

## Image storage

Images are stored as `bytea` in Postgres (`room_images` table, 5 MB cap). The
`app/services/image_service.py` module is the single abstraction point — migrate to
Azure Blob / S3 by changing only that module. See §8 of the plan doc for rationale
and migration path.

## Gap routes (from the original API coverage review)

All four flagged gap routes are implemented:

- `PATCH /api/v1/sessions/{id}` — rename / archive
- `POST /api/v1/upload/room-image` — dedicated room image upload
- `PATCH /api/v1/cart/{item_id}` — update quantity (1–10)
- `GET /api/v1/products/?category=&max_price=` — non-semantic SQL filter

## Verification checklist

1. `pytest tests/ -v` green
2. `alembic upgrade head` applies cleanly against a fresh Postgres
3. `python -m app.services.product_ingestion` populates 3,110 rows and creates the IVFFlat index
4. `GET /products/search?q=velvet+sofa` returns semantically relevant results
5. `GET /products/?category=Sofa&max_price=15000` returns filtered rows
6. Full scan → chat flow: upload → create session → `/chat/analyze` → `/chat/` returns carousel
7. `/visualize/` returns a composite PNG; `gpt-image-1` path preserves room scene
8. Cart: add → patch quantity → clear — counts match
9. `/chat/` returns 429 after 21 hits in 60s
10. `/healthz` and `/readyz` 200
11. Prod mode (`ENV=production`) disables `/docs`
