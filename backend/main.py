"""
Herald Relay – main FastAPI application entry point.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

load_dotenv()  # Load .env before anything else imports os.environ

from routers import billing, push, relay  # noqa: E402
from routers import hermes_proxy  # noqa: E402
from routers.tunnel import router as tunnel_router, monitor_router  # noqa: E402
from services.relay_manager import relay_manager  # noqa: E402
from services.firestore_client import get_db  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Herald Relay starting up …")
    try:
        get_db()  # warm up Firestore connection
        logger.info("Firestore connected")
    except Exception as e:
        logger.warning(f"Firestore not available (will retry on first request): {e}")
    yield
    logger.info("Herald Relay shutting down …")


app = FastAPI(
    title="Herald Relay API",
    description="Cloud relay service that tunnels Flutter ↔ Hermes over persistent WebSocket connections.",
    version="0.1.0",
    lifespan=lifespan,
)

# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Routers
# ---------------------------------------------------------------------------
app.include_router(relay.router, prefix="/relay")
app.include_router(hermes_proxy.router)
app.include_router(push.router)
app.include_router(billing.router)
app.include_router(tunnel_router, prefix="/tunnel")
app.include_router(monitor_router)


# ---------------------------------------------------------------------------
# Root / health
# ---------------------------------------------------------------------------


@app.get("/", tags=["meta"])
async def root():
    return {"message": "Herald Relay API", "docs": "/docs"}


@app.get("/health", tags=["meta"])
async def health():
    # Count BOTH transports: plugins connect over SSE (/tunnel/events), while
    # relay_manager only tracks WebSocket clients. Counting only the latter
    # made this read 0 with a perfectly healthy SSE tunnel.
    from routers import tunnel as _tunnel

    return {
        "status": "ok",
        "service": "herald-relay",
        "connected_devices": relay_manager.connected_count()
        + _tunnel.plugin_connected_count(),
        "ws_devices": relay_manager.connected_count(),
        "sse_devices": _tunnel.plugin_connected_count(),
    }
