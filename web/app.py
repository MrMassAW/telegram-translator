from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.sessions import SessionMiddleware
from web.routes import router
from web.admin_routes import router as admin_router
import os

from shared.config import SESSION_COOKIE_SECURE

app = FastAPI()


@app.middleware("http")
async def _no_store_admin_api(request: Request, call_next):
    """Operator console must not serve stale JSON after credit mutations (browser/proxy cache)."""
    response = await call_next(request)
    if request.url.path.startswith("/api/admin"):
        response.headers["Cache-Control"] = "no-store"
    return response


# CORS (inner); Session outermost — added last
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Session cookie for /admin.html operator console
_secret = os.getenv("SECRET_KEY", "").strip() or "dev-insecure-set-SECRET_KEY-in-env"
app.add_middleware(
    SessionMiddleware,
    secret_key=_secret,
    max_age=14 * 24 * 3600,
    same_site="lax",
    https_only=SESSION_COOKIE_SECURE,
)

# API Routes
app.include_router(router)
app.include_router(admin_router, prefix="/api")


@app.get("/health")
async def health():
    """Lightweight readiness check for load balancers (e.g. DigitalOcean App Platform)."""
    return {"status": "ok"}


# Static Files (Frontend)
static_dir = os.path.join(os.path.dirname(__file__), "static")
app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")
