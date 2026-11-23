"""
AutoApply - FastAPI Application Entry Point
Secured with proper CORS, rate limiting, and env-based config.
"""
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
import sys
from app.database import init_db
from config import settings

# Support --dry-run CLI argument
if "--dry-run" in sys.argv:
    settings.DRY_RUN = True

try:
    from slowapi import Limiter, _rate_limit_exceeded_handler
    from slowapi.util import get_remote_address
    from slowapi.errors import RateLimitExceeded
    HAS_SLOWAPI = True
except ImportError:
    HAS_SLOWAPI = False


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize database and start background scheduler on startup."""
    await init_db()
    try:
        from app.services.scheduler import start_scheduler, stop_scheduler
        start_scheduler(interval_hours=4)
    except Exception as e:
        print(f"Could not start scheduler: {e}")

    yield

    try:
        from app.services.scheduler import stop_scheduler
        stop_scheduler()
    except Exception:
        pass



app = FastAPI(
    title=settings.APP_NAME,
    version="2.0.0",
    lifespan=lifespan,
    redirect_slashes=False,
)

# CORS - locked to localhost only
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:8000", "http://127.0.0.1:8000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Rate limiting
if HAS_SLOWAPI:
    limiter = Limiter(key_func=get_remote_address)
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# Mount static files
app.mount("/static", StaticFiles(directory=settings.STATIC_DIR), name="static")

# Import and include routers
from app.routers import profile, jobs, applications, filters

app.include_router(profile.router, prefix="/api/profile", tags=["Profile"])
app.include_router(jobs.router, prefix="/api/jobs", tags=["Jobs"])
app.include_router(applications.router, prefix="/api/applications", tags=["Applications"])
app.include_router(filters.router, prefix="/api/filters", tags=["Filters"])


@app.get("/", include_in_schema=False)
async def serve_dashboard():
    """Serve the main dashboard HTML."""
    return FileResponse(f"{settings.STATIC_DIR}/index.html")
