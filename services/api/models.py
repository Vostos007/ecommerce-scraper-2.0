"""Pydantic models for API request/response schemas."""
from pydantic import BaseModel, Field, HttpUrl
from typing import Optional, List, Dict, Any
from datetime import datetime
from enum import Enum


class JobStatus(str, Enum):
    """Job execution status."""
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class JobOptions(BaseModel):
    """
    Job configuration options.
    
    Defines scraping behavior, limits, and feature flags.
    """
    domain: str = Field(..., description="Target domain")
    max_urls: int = Field(default=10000, ge=1, description="Maximum URLs to scrape")
    max_concurrency: int = Field(default=2, ge=1, le=10, description="Concurrent workers")
    allow_residential: bool = Field(default=False, description="Allow residential proxies")
    enable_firecrawl: bool = Field(default=False, description="Enable Firecrawl API")
    firecrawl_api_key: Optional[str] = Field(default=None, description="Firecrawl API key")
    traffic_budget_mb: int = Field(default=100, ge=1, description="Traffic budget in MB")
    residential_limit_mb: int = Field(default=50, ge=0, description="Residential proxy limit MB")


class CreateJobRequest(BaseModel):
    """
    Request to create a new scraping job.
    
    Either sitemap_url or sitemap_urls must be provided.
    """
    sitemap_url: Optional[HttpUrl] = Field(default=None, description="URL of sitemap.xml")
    sitemap_urls: Optional[List[str]] = Field(default=None, description="Direct list of URLs")
    options: JobOptions = Field(..., description="Job configuration")
    
    class Config:
        json_schema_extra = {
            "example": {
                "sitemap_url": "https://example.com/sitemap.xml",
                "options": {
                    "domain": "example.com",
                    "max_urls": 1000,
                    "max_concurrency": 2
                }
            }
        }


class JobResponse(BaseModel):
    """
    Job details response.
    
    Contains current status, metrics, and execution details.
    """
    id: str = Field(..., description="Job UUID")
    domain: str = Field(..., description="Target domain")
    status: JobStatus = Field(..., description="Current job status")
    created_at: datetime = Field(..., description="Job creation timestamp")
    started_at: Optional[datetime] = Field(default=None, description="Execution start time")
    finished_at: Optional[datetime] = Field(default=None, description="Execution finish time")
    total_urls: int = Field(default=0, description="Total URLs to process")
    success_urls: int = Field(default=0, description="Successfully scraped URLs")
    failed_urls: int = Field(default=0, description="Failed URLs")
    traffic_mb_used: float = Field(default=0.0, description="Traffic consumed in MB")
    residential_mb_used: float = Field(default=0.0, description="Residential proxy traffic MB")
    error_message: Optional[str] = Field(default=None, description="Error message if failed")
    
    class Config:
        json_schema_extra = {
            "example": {
                "id": "550e8400-e29b-41d4-a716-446655440000",
                "domain": "example.com",
                "status": "running",
                "created_at": "2025-10-01T19:30:00Z",
                "started_at": "2025-10-01T19:30:05Z",
                "total_urls": 100,
                "success_urls": 45,
                "failed_urls": 0,
                "traffic_mb_used": 12.5,
                "residential_mb_used": 0.0
            }
        }


class ExportType(str, Enum):
    """Export data type."""
    FULL = "full"
    SEO = "seo"
    DIFF = "diff"


class ExportFormat(str, Enum):
    """Export file format."""
    CSV = "csv"
    XLSX = "xlsx"
    JSON = "json"


class ExportResponse(BaseModel):
    """
    Export artifact response.
    
    Contains download URL and metadata for generated exports.
    """
    id: str = Field(..., description="Export UUID")
    job_id: str = Field(..., description="Parent job UUID")
    type: ExportType = Field(..., description="Export data type")
    format: ExportFormat = Field(..., description="File format")
    url: str = Field(..., description="Download URL")
    size_bytes: int = Field(..., description="File size in bytes")
    created_at: datetime = Field(..., description="Export generation timestamp")
    
    class Config:
        json_schema_extra = {
            "example": {
                "id": "660e8400-e29b-41d4-a716-446655440000",
                "job_id": "550e8400-e29b-41d4-a716-446655440000",
                "type": "full",
                "format": "csv",
                "url": "/api/downloads/export-123.csv",
                "size_bytes": 1048576,
                "created_at": "2025-10-01T19:45:00Z"
            }
        }


class HealthResponse(BaseModel):
    """Health check response."""
    status: str = Field(..., description="Overall health status")
    database: str = Field(..., description="Database connection status")
    
    class Config:
        json_schema_extra = {
            "example": {
                "status": "ok",
                "database": "ok"
            }
        }


class ErrorResponse(BaseModel):
    """Standard error response."""
    detail: str = Field(..., description="Error message")
    
    class Config:
        json_schema_extra = {
            "example": {
                "detail": "Job not found"
            }
        }