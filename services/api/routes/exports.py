"""Export artifacts endpoints."""
from fastapi import APIRouter, Depends, HTTPException
from datetime import datetime, timezone, timedelta
from typing import List, Optional

from ..dependencies import get_db

from ..models import ExportResponse, ExportStatusResponse
from database.manager import DatabaseManager


router = APIRouter()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _calculate_eta(
    total: Optional[int],
    processed: int,
    started_at: Optional[datetime],
) -> tuple[Optional[float], Optional[datetime]]:
    if not total or total <= 0:
        return None, None
    remaining = max(total - processed, 0)
    if remaining == 0:
        return 0.0, _now()

    if not started_at:
        return None, None

    elapsed = (_now() - started_at).total_seconds()
    if elapsed <= 0 or processed <= 0:
        return None, None

    speed = processed / elapsed
    if speed <= 0:
        return None, None

    seconds_left = remaining / speed
    eta = _now() + timedelta(seconds=seconds_left)
    return seconds_left, eta


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


@router.get("/export/status/{job_id}", response_model=ExportStatusResponse)
async def get_export_status(
    job_id: str,
    db: DatabaseManager = Depends(get_db)
):
    """Return detailed progress information for the specified export job."""

    job = await db.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    status = job.get('status', 'unknown')
    total_urls = job.get('total_urls') or 0
    success_urls = job.get('success_urls') or 0
    failed_urls = job.get('failed_urls') or 0
    processed_urls = success_urls + failed_urls

    started_at: Optional[datetime] = job.get('started_at') or job.get('created_at')
    finished_at: Optional[datetime] = job.get('finished_at')
    eta_seconds, eta_timestamp = _calculate_eta(total_urls, processed_urls, started_at)

    progress_percent: float
    if total_urls and total_urls > 0:
        progress_percent = min(100.0, (processed_urls / total_urls) * 100.0)
    elif status in {'succeeded', 'failed', 'cancelled'}:
        progress_percent = 100.0
    else:
        progress_percent = 0.0

    domain = job.get('domain') or 'unknown-site'
    fallback_script = f"{domain.replace('.', '_')}_fast_export"
    script = job.get('options', {}).get('script', fallback_script) if isinstance(job.get('options'), dict) else fallback_script

    return ExportStatusResponse(
        jobId=job_id,
        site=domain,
        script=script,
        status=status,
        startedAt=started_at,
        lastEventAt=finished_at or started_at,
        exitCode=job.get('exit_code'),
        exitSignal=None,
        totalUrls=total_urls if total_urls else None,
        processedUrls=processed_urls,
        successUrls=success_urls,
        failedUrls=failed_urls,
        progressPercent=round(progress_percent, 2),
        estimatedSecondsRemaining=eta_seconds,
        estimatedCompletionAt=eta_timestamp,
    )
