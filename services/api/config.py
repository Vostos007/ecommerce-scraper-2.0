"""Configuration using pydantic-settings."""
from pydantic_settings import BaseSettings
from functools import lru_cache
from typing import List


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""
    
    # Database
    database_url: str = "postgresql://scraper:scraper@localhost:5432/scraper"
    
    # Redis
    redis_url: str = "redis://localhost:6379/0"
    
    # CORS
    cors_origins: List[str] = ["http://localhost:3000"]
    
    # Auth
    admin_token: str = "dev-admin-token"
    
    # S3/MinIO
    s3_endpoint: str = "http://localhost:9000"
    s3_bucket: str = "scraper-artifacts"
    s3_access_key: str = "minioadmin"
    s3_secret_key: str = "minioadmin"
    
    # FlareSolverr
    flaresolverr_url: str = "http://localhost:8191"
    
    class Config:
        env_file = ".env"
        case_sensitive = False


@lru_cache
def get_settings() -> Settings:
    """
    Get cached settings instance.
    
    Returns:
        Settings: Application settings
    """
    return Settings()