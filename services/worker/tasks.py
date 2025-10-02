"""RQ task definitions."""
import asyncio
from .job_executor import JobExecutor


def scrape_job_task(job_id: str, urls: list[str], options: dict):
    """
    Main scraping task (executed by RQ worker).
    
    This is the entry point for RQ workers. It receives job parameters,
    creates a JobExecutor instance, and runs the async execution logic
    in a new event loop.
    
    Args:
        job_id: UUID of the job in database
        urls: List of URLs to scrape
        options: Job configuration dict
        
    Returns:
        dict: Execution result with status and counts
        
    Raises:
        Exception: Any error during job execution
        
    Example:
        This function is called by RQ workers, not directly:
        ```python
        # Enqueued by API
        job_queue.enqueue(
            scrape_job_task,
            job_id="550e8400...",
            urls=["https://example.com/page1"],
            options={"domain": "example.com"}
        )
        ```
    """
    print(f"[RQ Task] Starting job {job_id} with {len(urls)} URLs")
    
    executor = JobExecutor(job_id, urls, options)
    
    # Run async executor in sync context
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    try:
        result = loop.run_until_complete(executor.run())
        return result
    finally:
        loop.close()