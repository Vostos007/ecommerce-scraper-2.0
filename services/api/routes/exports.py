"""Export artifacts endpoints."""
from fastapi import APIRouter, Depends, HTTPException
from typing import List

from ..dependencies import get_db
from ..models import ExportResponse
from database.manager import DatabaseManager


router = APIRouter()


@router.get("/jobs/{job_id}/exports", response_model=List[ExportResponse])
async def list_exports(
    job_id: str,
    db: DatabaseManager = Depends(get_db)
):
    """
    List all export artifacts for a job.
    
    Returns generated exports (CSV, XLSX, JSON) for the specified job.
    Each export has a download URL and metadata.
    
    Args:
        job_id: UUID of the job
        db: Database manager (injected)
        
    Returns:
        List[ExportResponse]: List of export artifacts
        
    Raises:
        HTTPException: 404 if job not found
        
    Example response:
        ```json
        [
            {
                "id": "export-uuid-1",
                "job_id": "job-uuid",
                "type": "full",
                "format": "csv",
                "url": "/api/downloads/export-123.csv",
                "size_bytes": 1048576,
                "created_at": "2025-10-01T19:45:00Z"
            },
            {
                "id": "export-uuid-2",
                "job_id": "job-uuid",
                "type": "seo",
                "format": "xlsx",
                "url": "/api/downloads/export-456.xlsx",
                "size_bytes": 2097152,
                "created_at": "2025-10-01T19:45:10Z"
            }
        ]
        ```
    """
    # Verify job exists
    job = await db.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    
    # Get exports
    exports = await db.get_job_exports(job_id)
    
    # Transform to response models
    return [
        ExportResponse(
            id=exp['id'],
            job_id=exp['job_id'],
            type=exp['type'],
            format=exp['format'],
            url=f"/api/downloads/{exp['path']}",  # Generate download URL
            size_bytes=exp['size_bytes'] or 0,
            created_at=exp['created_at']
        )
        for exp in exports
    ]