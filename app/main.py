from contextlib import asynccontextmanager

import structlog
import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from app.config import settings
from app.extractor import init_extractor
from app.llm_client import LLMClient
from app.memory import init_store
from app.proxy import limiter, router

logger = structlog.get_logger()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize and cleanup shared resources."""
    # Startup
    store = init_store()
    app.state.store = store

    llm_client = LLMClient()
    await llm_client.init()
    app.state.llm_client = llm_client

    executor = init_extractor(store)
    app.state.executor = executor

    logger.info(
        "app_started",
        host=settings.host,
        port=settings.port,
    )

    yield

    # Shutdown
    await llm_client.close()
    logger.info("app_shutdown")


app = FastAPI(
    title="SAGA-MEM RP Memory Engine",
    description="RisuAI multi-provider LLM proxy with LangMem episode memory",
    version="0.2.0",
    lifespan=lifespan,
)

# CORS for RisuAI
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Rate limiter
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# Mount proxy router
app.include_router(router)


@app.get("/health")
async def health():
    """Health check endpoint."""
    return {"status": "ok", "version": "0.2.0"}


@app.get("/debug/store")
async def debug_store():
    """Debug: dump all items in the store. DEV ONLY."""
    store = app.state.store
    data = {}
    for namespace, items in store._data.items():
        ns_key = "/".join(namespace)
        data[ns_key] = {k: v.value for k, v in items.items()}
    return {
        "total_namespaces": len(data),
        "namespaces": list(data.keys()),
        "items": data,
    }


if __name__ == "__main__":
    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        reload=True,
    )
