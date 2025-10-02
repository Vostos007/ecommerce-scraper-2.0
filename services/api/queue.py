"""RQ (Redis Queue) integration."""
import os
from redis import Redis
from rq import Queue


# Initialize Redis connection
redis_conn = Redis.from_url(os.getenv("REDIS_URL", "redis://localhost:6379/0"))

# Create job queue
job_queue = Queue("scraping", connection=redis_conn)


def enqueue_scrape_job(job_id: str, urls: list[str], options: dict) -> str:
    """
    Enqueue scraping job to worker pool.
    
    Creates an RQ job that will be picked up by available workers.
    The job is placed in the 'scraping' queue with specified timeout
    and TTL settings.
    
    Args:
        job_id: UUID of the job in database
        urls: List of URLs to scrape
        options: Job configuration options
        
    Returns:
        str: RQ job ID (for tracking in Redis)
        
    Example:
        ```python
        rq_job_id = enqueue_scrape_job(
            job_id="550e8400-e29b-41d4-a716-446655440000",
            urls=["https://example.com/page1", "https://example.com/page2"],
            options={"domain": "example.com", "max_concurrency": 2}
        )
        print(f"Enqueued as RQ job: {rq_job_id}")
        ```
    
    Notes:
        - Job timeout: 2 hours (for large scraping jobs)
        - Result TTL: 24 hours (keep results for debugging)
        - Failure TTL: 7 days (keep failures for analysis)
    """
    from services.worker.tasks import scrape_job_task
    
    rq_job = job_queue.enqueue(
        scrape_job_task,
        job_id=job_id,
        urls=urls,
        options=options,
        job_timeout="2h",
        result_ttl=86400,  # 24h
        failure_ttl=604800  # 7d
    )
    
    return rq_job.id