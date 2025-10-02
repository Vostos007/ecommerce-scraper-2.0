"""RQ Worker process."""
import os
from redis import Redis
from rq import Worker


def main():
    """
    Start RQ worker listening to 'scraping' queue.
    
    The worker will:
    - Connect to Redis using REDIS_URL environment variable
    - Listen to the 'scraping' queue for jobs
    - Execute tasks from services.worker.tasks module
    - Run with scheduler enabled for periodic tasks
    
    Environment variables:
        REDIS_URL: Redis connection URL (default: redis://localhost:6379/0)
    
    Usage:
        python services/worker/worker.py
    """
    redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    redis_conn = Redis.from_url(redis_url)
    
    worker = Worker(
        ["scraping"],
        connection=redis_conn,
        name=f"worker-{os.getpid()}"
    )
    
    print(f"🚀 Worker started: {worker.name}")
    print(f"📡 Listening to queue: scraping")
    print(f"🔗 Redis: {redis_url}")
    
    worker.work(with_scheduler=True)


if __name__ == "__main__":
    main()