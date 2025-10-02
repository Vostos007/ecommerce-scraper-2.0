"""FastAPI dependencies."""
from fastapi import Request
from database.manager import DatabaseManager


async def get_db(request: Request) -> DatabaseManager:
    """
    Get database manager from app state.
    
    This dependency injects the DatabaseManager instance into route handlers.
    The manager is initialized during application startup and stored in app.state.
    
    Args:
        request: FastAPI request object
        
    Returns:
        DatabaseManager: Database connection pool manager
        
    Example usage:
        ```python
        @router.get("/jobs")
        async def list_jobs(db: DatabaseManager = Depends(get_db)):
            jobs = await db.list_jobs()
            return jobs
        ```
    """
    return request.app.state.db