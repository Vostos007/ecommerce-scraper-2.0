"""FastAPI Backend for Webscraper."""
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager

from .config import get_settings
from .routes import jobs, health, sse, exports
from database.manager import DatabaseManager


# Get settings
settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Application lifespan manager.
    
    Handles startup and shutdown events:
    - Startup: Initialize database connection pool
    - Shutdown: Close database connections gracefully
    
    Args:
        app: FastAPI application instance
    """
    # Startup
    print("[API] Starting up...")
    print(f"[API] Database URL: {settings.database_url}")
    print(f"[API] Redis URL: {settings.redis_url}")
    print(f"[API] CORS Origins: {settings.cors_origins}")
    
    # Initialize database manager
    app.state.db = DatabaseManager(settings.database_url)
    await app.state.db.init_pool()
    print("[API] Database pool initialized")
    
    yield
    
    # Shutdown
    print("[API] Shutting down...")
    await app.state.db.close()
    print("[API] Database pool closed")


# Create FastAPI application
app = FastAPI(
    title="Webscraper API",
    version="1.0.0",
    description="""
    FastAPI backend service for web scraping orchestration.
    
    Features:
    - Job creation and management
    - Worker pool integration via RQ (Redis Queue)
    - Real-time job monitoring via SSE
    - Export artifact management
    - Health monitoring
    
    Architecture:
    - Backend: FastAPI + asyncpg + RQ
    - Database: PostgreSQL
    - Queue: Redis
    - Workers: RQ workers (separate processes)
    """,
    lifespan=lifespan,
    docs_url="/api/docs",
    redoc_url="/api/redoc",
    openapi_url="/api/openapi.json"
)


# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Include routers
app.include_router(
    jobs.router,
    prefix="/api/jobs",
    tags=["jobs"]
)
app.include_router(
    exports.router,
    prefix="/api",
    tags=["exports"]
)
app.include_router(
    health.router,
    prefix="/api",
    tags=["health"]
)
app.include_router(
    sse.router,
    prefix="/api",
    tags=["sse"]
)


@app.get("/")
def root():
    """
    Root endpoint.
    
    Returns basic service information.
    
    Returns:
        dict: Service status and name
        
    Example response:
        ```json
        {
            "status": "ok",
            "service": "webscraper-api",
            "version": "1.0.0",
            "docs": "/api/docs"
        }
        ```
    """
    return {
        "status": "ok",
        "service": "webscraper-api",
        "version": "1.0.0",
        "docs": "/api/docs"
    }


@app.get("/api")
def api_root():
    """
    API root endpoint.
    
    Returns available API endpoints and documentation links.
    
    Returns:
        dict: API information
        
    Example response:
        ```json
        {
            "message": "Webscraper API",
            "version": "1.0.0",
            "endpoints": {
                "jobs": "/api/jobs",
                "health": "/api/health",
                "docs": "/api/docs"
            }
        }
        ```
    """
    return {
        "message": "Webscraper API",
        "version": "1.0.0",
        "endpoints": {
            "jobs": "/api/jobs",
            "exports": "/api/jobs/{job_id}/exports",
            "stream": "/api/jobs/{job_id}/stream",
            "health": "/api/health",
            "docs": "/api/docs",
            "redoc": "/api/redoc"
        }
    }


if __name__ == "__main__":
    import uvicorn
    
    # Run development server
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
        log_level="info"
    )