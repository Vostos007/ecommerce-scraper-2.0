"""Server-Sent Events for live logs."""
from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
import asyncio
import json

from ..dependencies import get_db
from database.manager import DatabaseManager


router = APIRouter()


@router.get("/jobs/{job_id}/stream")
async def stream_job_logs(
    job_id: str,
    db: DatabaseManager = Depends(get_db)
):
    """
    Stream job logs via SSE.
    
    Provides real-time updates about job progress using Server-Sent Events.
    Clients can listen to this stream to display live progress to users.
    
    Args:
        job_id: UUID of the job to stream
        db: Database manager (injected)
        
    Returns:
        StreamingResponse: SSE stream of job events
        
    Event types:
        - progress: Job progress update with URL count and percentage
        - complete: Job finished successfully
        - error: Job encountered an error
        
    Example SSE events:
        ```
        data: {"type": "progress", "data": {"job_id": "...", "message": "Processing URL 1/5", "progress": 20}}
        
        data: {"type": "progress", "data": {"job_id": "...", "message": "Processing URL 2/5", "progress": 40}}
        
        data: {"type": "complete"}
        ```
    
    TODO:
        - Subscribe to Redis pub/sub channel for real job logs
        - Forward worker progress events to SSE stream
        - Add heartbeat events to keep connection alive
    """
    
    async def event_generator():
        """Generate SSE events."""
        # TODO: Subscribe to Redis pub/sub channel for job logs
        # redis_client = Redis.from_url(settings.redis_url)
        # pubsub = redis_client.pubsub()
        # pubsub.subscribe(f"job:{job_id}:logs")
        
        # For now, emit placeholder events
        for i in range(5):
            event = {
                "type": "progress",
                "data": {
                    "job_id": job_id,
                    "message": f"Processing URL {i+1}/5",
                    "progress": (i+1) * 20
                }
            }
            yield f"data: {json.dumps(event)}\n\n"
            await asyncio.sleep(1)
        
        yield f"data: {json.dumps({'type': 'complete'})}\n\n"
    
    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no"  # Disable nginx buffering
        }
    )