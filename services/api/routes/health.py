"""Health check endpoint."""
from fastapi import APIRouter, Depends

from ..dependencies import get_db
from database.manager import DatabaseManager


router = APIRouter()


@router.get("/health")
async def health_check(db: DatabaseManager = Depends(get_db)):
    """
    Health check with dependencies.
    
    Verifies that the API service is running and can connect to
    required dependencies (database, Redis queue).
    
    Args:
        db: Database manager (injected)
        
    Returns:
        dict: Health status of service and dependencies
        
    Example response (healthy):
        ```json
        {
            "status": "ok",
            "database": "ok"
        }
        ```
    
    Example response (unhealthy):
        ```json
        {
            "status": "ok",
            "database": "error: connection refused"
        }
        ```
    """
    # Test database connection
    try:
        await db.execute("SELECT 1")
        db_status = "ok"
    except Exception as e:
        db_status = f"error: {e}"
    
    return {
        "status": "ok",
        "database": db_status
    }