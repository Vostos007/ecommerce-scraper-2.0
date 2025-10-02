"""Job management endpoints."""
from fastapi import APIRouter, Depends, HTTPException
from typing import List, Optional
import uuid

from ..dependencies import get_db
from ..models import CreateJobRequest, JobResponse
from .. import queue
from database.manager import DatabaseManager


router = APIRouter()


@router.post("", response_model=JobResponse, status_code=201)
async def create_job(
    req: CreateJobRequest,
    db: DatabaseManager = Depends(get_db)
):
    """
    Create a new scraping job.
    
    Accepts either a sitemap URL to parse or a direct list of URLs.
    Creates a job record in the database and enqueues it for processing
    by the worker pool.
    
    Args:
        req: Job creation request with sitemap and options
        db: Database manager (injected)
        
    Returns:
        JobResponse: Created job details with UUID
        
    Raises:
        HTTPException: 400 if neither sitemap_url nor sitemap_urls provided
        
    Example request:
        ```json
        {
            "sitemap_url": "https://example.com/sitemap.xml",
            "options": {
                "domain": "example.com",
                "max_urls": 1000,
                "max_concurrency": 2,
                "allow_residential": false
            }
        }
        ```
    
    Example response:
        ```json
        {
            "id": "550e8400-e29b-41d4-a716-446655440000",
            "domain": "example.com",
            "status": "queued",
            "created_at": "2025-10-01T19:30:00Z",
            "total_urls": 1000,
            "success_urls": 0,
            "failed_urls": 0,
            "traffic_mb_used": 0.0,
            "residential_mb_used": 0.0
        }
        ```
    """
    # Parse sitemap or use direct URLs
    urls = []
    
    if req.sitemap_url:
        # TODO: Parse sitemap using utils/sitemap_parser.py
        # For now, placeholder - in Phase 4 implement actual sitemap parsing
        # from utils.sitemap_parser import parse_sitemap
        # urls = await parse_sitemap(str(req.sitemap_url))
        raise HTTPException(
            status_code=501,
            detail="Sitemap parsing not yet implemented. Use sitemap_urls instead."
        )
    elif req.sitemap_urls:
        urls = req.sitemap_urls
    else:
        raise HTTPException(
            status_code=400,
            detail="Either sitemap_url or sitemap_urls required"
        )
    
    # Generate job ID
    job_id = str(uuid.uuid4())
    
    # Create job record
    await db.create_job(
        job_id=job_id,
        domain=req.options.domain,
        options=req.options.model_dump(),
        total_urls=len(urls)
    )
    
    # Enqueue to worker pool
    rq_job_id = queue.enqueue_scrape_job(
        job_id,
        urls,
        req.options.model_dump()
    )
    
    print(f"[API] Created job {job_id}, enqueued as RQ job {rq_job_id}")
    
    # Return created job
    job = await db.get_job(job_id)
    return JobResponse(**job)


@router.get("/{job_id}", response_model=JobResponse)
async def get_job(
    job_id: str,
    db: DatabaseManager = Depends(get_db)
):
    """
    Get job status and details.
    
    Retrieves current status, progress metrics, and execution details
    for a specific job.
    
    Args:
        job_id: UUID of the job
        db: Database manager (injected)
        
    Returns:
        JobResponse: Job details and current status
        
    Raises:
        HTTPException: 404 if job not found
        
    Example response:
        ```json
        {
            "id": "550e8400-e29b-41d4-a716-446655440000",
            "domain": "example.com",
            "status": "running",
            "created_at": "2025-10-01T19:30:00Z",
            "started_at": "2025-10-01T19:30:05Z",
            "total_urls": 100,
            "success_urls": 45,
            "failed_urls": 2,
            "traffic_mb_used": 12.5,
            "residential_mb_used": 0.0
        }
        ```
    """
    job = await db.get_job(job_id)
    
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    
    return JobResponse(**job)


@router.post("/{job_id}/cancel", status_code=202)
async def cancel_job(
    job_id: str,
    db: DatabaseManager = Depends(get_db)
):
    """
    Cancel a running or queued job.
    
    Attempts to cancel job execution. Jobs in 'queued' or 'running'
    status can be cancelled. Already completed jobs cannot be cancelled.
    
    Args:
        job_id: UUID of the job to cancel
        db: Database manager (injected)
        
    Returns:
        dict: {"ok": true} on success
        
    Raises:
        HTTPException: 404 if job not found
        HTTPException: 409 if job cannot be cancelled (already completed)
        
    Example response:
        ```json
        {
            "ok": true
        }
        ```
    
    TODO:
        - Send cancel signal to RQ job using job_queue.cancel(rq_job_id)
        - Worker should check for cancellation and cleanup gracefully
    """
    job = await db.get_job(job_id)
    
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    
    if job["status"] not in ["queued", "running"]:
        raise HTTPException(
            status_code=409,
            detail=f"Cannot cancel job in status: {job['status']}"
        )
    
    # TODO: Send cancel signal to RQ job
    # rq_job = job_queue.fetch_job(rq_job_id)
    # if rq_job:
    #     rq_job.cancel()
    
    # Update job status
    await db.update_job_status(job_id, "cancelled")
    
    print(f"[API] Cancelled job {job_id}")
    
    return {"ok": True}


@router.get("", response_model=List[JobResponse])
async def list_jobs(
    domain: Optional[str] = None,
    limit: int = 50,
    db: DatabaseManager = Depends(get_db)
):
    """
    List jobs with optional filtering.
    
    Returns a list of jobs, optionally filtered by domain.
    Results are sorted by creation time (newest first).
    
    Args:
        domain: Optional domain filter
        limit: Maximum number of jobs to return (default 50, max 100)
        db: Database manager (injected)
        
    Returns:
        List[JobResponse]: List of jobs
        
    Example response:
        ```json
        [
            {
                "id": "job-uuid-1",
                "domain": "example.com",
                "status": "succeeded",
                "created_at": "2025-10-01T19:30:00Z",
                "finished_at": "2025-10-01T19:45:00Z",
                "total_urls": 100,
                "success_urls": 98,
                "failed_urls": 2
            },
            {
                "id": "job-uuid-2",
                "domain": "example.com",
                "status": "running",
                "created_at": "2025-10-01T19:00:00Z",
                "started_at": "2025-10-01T19:00:05Z",
                "total_urls": 500,
                "success_urls": 250
            }
        ]
        ```
    """
    # Limit validation
    if limit > 100:
        limit = 100
    
    jobs = await db.list_jobs(domain=domain, limit=limit)
    
    return [JobResponse(**j) for j in jobs]