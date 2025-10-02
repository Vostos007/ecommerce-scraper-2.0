"""Job execution orchestrator."""
import os
from pathlib import Path
from datetime import datetime
import hashlib

from database.manager import DatabaseManager
from core.scraper_engine import ScraperEngine
from utils.export_writers import ExportWriter
from utils.export_schema import FullCSVRow, SEOCSVRow, DiffCSVRow


class JobExecutor:
    """Orchestrates scraping job execution."""
    
    def __init__(self, job_id: str, urls: list[str], options: dict):
        """
        Initialize job executor.
        
        Args:
            job_id: UUID of the job
            urls: List of URLs to scrape
            options: Job configuration options
        """
        self.job_id = job_id
        self.urls = urls
        self.options = options
        self.db = DatabaseManager(os.getenv("DATABASE_URL"))
        self.domain = options.get("domain", "unknown")
    
    async def run(self) -> dict:
        """Execute job."""
        await self.db.init_pool()
        
        try:
            # Update status to running
            await self.db.update_job_status(
                self.job_id, 
                "running",
                started_at=datetime.utcnow()
            )
            
            # Initialize scraper
            scraper = ScraperEngine(
                domain=self.domain,
                max_workers=self.options.get("max_concurrency", 2)
            )
            
            results = []
            success_count = 0
            failed_count = 0
            
            # Scrape URLs
            for i, url in enumerate(self.urls):
                print(f"[JobExecutor] Processing {i+1}/{len(self.urls)}: {url}")
                
                try:
                    result = await scraper.scrape_url(url)
                    results.append(result)
                    
                    # Determine success/failure
                    status_code = result.get("status_code", 0)
                    if 200 <= status_code < 300:
                        success_count += 1
                    else:
                        failed_count += 1
                    
                    # Store result in database
                    await self.db.insert_page_result(self.job_id, {
                        "url": url,
                        "final_url": result.get("final_url", url),
                        "http_status": status_code,
                        "title": result.get("data", {}).get("title"),
                        "h1": result.get("data", {}).get("h1"),
                        "content_hash": self._hash_content(result.get("html", "")),
                        "bytes_in": len(result.get("html", "")),
                        "data_full": result.get("data"),
                        "data_seo": result.get("seo_data"),
                        "strategy_used": result.get("strategy"),
                        "proxy_used": result.get("proxy"),
                        "error_class": result.get("error_class"),
                        "error_message": result.get("error_message"),
                        "retry_count": result.get("retry_count", 0)
                    })
                    
                    # Update job progress
                    await self.db.update_job_status(
                        self.job_id,
                        "running",
                        success_urls=success_count,
                        failed_urls=failed_count
                    )
                    
                except Exception as e:
                    print(f"[JobExecutor] Error processing {url}: {e}")
                    failed_count += 1
                    await self.db.insert_page_result(self.job_id, {
                        "url": url,
                        "http_status": 0,
                        "error_class": type(e).__name__,
                        "error_message": str(e)
                    })
            
            # Generate exports
            await self.generate_exports(results)
            
            # Mark as succeeded
            await self.db.update_job_status(
                self.job_id, 
                "succeeded",
                finished_at=datetime.utcnow(),
                success_urls=success_count,
                failed_urls=failed_count
            )
            
            return {
                "status": "success", 
                "results_count": len(results),
                "success_count": success_count,
                "failed_count": failed_count
            }
            
        except Exception as e:
            print(f"[JobExecutor] Fatal error: {e}")
            await self.db.update_job_status(
                self.job_id,
                "failed",
                finished_at=datetime.utcnow(),
                error_message=str(e)
            )
            raise
        
        finally:
            await self.db.close()
    
    async def generate_exports(self, results: list):
        """Generate CSV/XLSX exports."""
        # Get job pages from database
        pages = await self.db.get_job_pages(self.job_id)
        
        # Build export data
        full_rows = []
        seo_rows = []
        
        for page in pages:
            # Full CSV row
            full_rows.append(FullCSVRow(
                url=page["url"],
                final_url=page.get("final_url"),
                http_status=page.get("http_status", 0),
                fetched_at=page.get("fetched_at", datetime.utcnow()).isoformat(),
                title=page.get("title"),
                h1=page.get("data_full", {}).get("h1") if page.get("data_full") else None,
                price=page.get("data_full", {}).get("price") if page.get("data_full") else None,
                currency=page.get("data_full", {}).get("currency") if page.get("data_full") else None,
                availability=page.get("data_full", {}).get("availability") if page.get("data_full") else None,
                sku=page.get("data_full", {}).get("sku") if page.get("data_full") else None,
                brand=page.get("data_full", {}).get("brand") if page.get("data_full") else None,
                category=page.get("data_full", {}).get("category") if page.get("data_full") else None,
                breadcrumbs=page.get("data_full", {}).get("breadcrumbs") if page.get("data_full") else None,
                images="|".join(page.get("data_full", {}).get("images", [])) if page.get("data_full") else None,
                attrs_json=str(page.get("data_full", {}).get("attributes", {})) if page.get("data_full") else None,
                text_hash=page.get("content_hash")
            ))
            
            # SEO CSV row
            seo_rows.append(SEOCSVRow(
                url=page["url"],
                fetched_at=page.get("fetched_at", datetime.utcnow()).isoformat(),
                title=page.get("title"),
                meta_description=page.get("data_seo", {}).get("meta_description") if page.get("data_seo") else None,
                h1=page.get("data_seo", {}).get("h1") if page.get("data_seo") else None,
                og_title=page.get("data_seo", {}).get("og_title") if page.get("data_seo") else None,
                og_description=page.get("data_seo", {}).get("og_description") if page.get("data_seo") else None,
                og_image=page.get("data_seo", {}).get("og_image") if page.get("data_seo") else None,
                twitter_title=page.get("data_seo", {}).get("twitter_title") if page.get("data_seo") else None,
                twitter_description=page.get("data_seo", {}).get("twitter_description") if page.get("data_seo") else None,
                canonical=page.get("data_seo", {}).get("canonical") if page.get("data_seo") else None,
                robots=page.get("data_seo", {}).get("robots") if page.get("data_seo") else None,
                hreflang=page.get("data_seo", {}).get("hreflang") if page.get("data_seo") else None,
                images_alt_joined="|".join(page.get("data_seo", {}).get("images_alt", [])) if page.get("data_seo") else None
            ))
        
        # Write exports
        export_dir = Path(f"data/jobs/{self.job_id}")
        export_dir.mkdir(parents=True, exist_ok=True)
        
        writer = ExportWriter()
        
        # Full export
        full_csv_path = export_dir / "full.csv"
        writer.write_csv(full_rows, full_csv_path)
        await self.db.register_export(
            self.job_id, 
            "full", 
            "csv", 
            str(full_csv_path),
            full_csv_path.stat().st_size
        )
        
        # SEO export
        seo_csv_path = export_dir / "seo.csv"
        writer.write_csv(seo_rows, seo_csv_path)
        await self.db.register_export(
            self.job_id, 
            "seo", 
            "csv", 
            str(seo_csv_path),
            seo_csv_path.stat().st_size
        )
        
        # Diff export (compare with last successful job for domain)
        diff_rows = await self.generate_diff()
        if diff_rows:
            diff_csv_path = export_dir / "diff.csv"
            writer.write_csv(diff_rows, diff_csv_path)
            await self.db.register_export(
                self.job_id, 
                "diff", 
                "csv", 
                str(diff_csv_path),
                diff_csv_path.stat().st_size
            )
    
    async def generate_diff(self) -> list[DiffCSVRow]:
        """Generate diff by comparing with previous successful job."""
        # Get snapshots for domain
        snapshots = await self.db.get_snapshots_for_domain(self.domain)
        current_pages = await self.db.get_job_pages(self.job_id)
        
        # Build diff rows
        diff_rows = []
        
        # TODO: Implement diff logic (compare current vs snapshots)
        # For now, return empty list
        
        return diff_rows
    
    def _hash_content(self, html: str) -> str:
        """Generate SHA-256 hash of HTML content."""
        return hashlib.sha256(html.encode()).hexdigest()