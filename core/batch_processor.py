from typing import List, Dict, Any, Optional
from tqdm import tqdm
from datetime import datetime
import time
import random
from urllib.parse import urlparse

from core.hybrid_engine import HybridScrapingEngine
from utils.logger import setup_logger
from utils.system_monitor import get_system_resources

logger = setup_logger(__name__)


class BatchProcessor:
    def __init__(self, db_manager, config=None):
        self.db_manager = db_manager
        self.config = config
        self.engine = HybridScrapingEngine()
        self.retry_queues = {
            "network_error": [],
            "parse_error": [],
            "db_error": [],
            "batch_error": [],
        }
        self.retry_counts = {
            "network_error": 0,
            "parse_error": 0,
            "db_error": 0,
            "batch_error": 0,
        }
        self.performance_history = []
        self.initial_batch_size = (
            self.config.get("batch_processing", {}).get("batch_size", 50)
            if self.config
            else 50
        )

    def calculate_optimal_batch_size(
        self, total_urls: int, system_resources: Dict[str, float]
    ) -> int:
        """
        Dynamically calculate optimal batch size based on system resources.
        """
        memory_available = system_resources.get("memory_percent", 0)
        cpu_usage = system_resources.get("cpu_percent", 0)
        network_capacity = system_resources.get(
            "network", 1.0
        )  # Placeholder for network metric

        # Conservative base
        base_size = self.initial_batch_size

        # Adjust for memory: reduce if >80%
        if memory_available > 80:
            base_size = max(10, int(base_size * (100 - memory_available) / 20))

        # Adjust for CPU: reduce if >90%
        if cpu_usage > 90:
            base_size = max(10, int(base_size * (100 - cpu_usage) / 10))

        # Adjust for network: scale up if high capacity
        if network_capacity > 1.0:
            base_size = min(100, int(base_size * network_capacity))

        # Adapt based on historical performance
        if self.performance_history:
            avg_success_rate = sum(
                h["success_rate"] for h in self.performance_history[-5:]
            ) / min(5, len(self.performance_history))
            if avg_success_rate < 0.8:
                base_size = max(10, int(base_size * 0.8))

        optimal_size = max(10, min(100, base_size))
        logger.info(
            f"Calculated optimal batch size: {optimal_size} (memory: {memory_available}%, CPU: {cpu_usage}%)"
        )
        return optimal_size

    def split_into_batches(self, urls: List[str], batch_size: int) -> List[List[str]]:
        """
        Split URL list into batches.
        """
        return [urls[i : i + batch_size] for i in range(0, len(urls), batch_size)]

    def track_batch_performance(
        self,
        batch_num: int,
        urls_count: int,
        successes: int,
        errors: int,
        start_time: float,
    ) -> Dict[str, Any]:
        """
        Track and return performance metrics for a batch.
        """
        end_time = time.time()
        duration = end_time - start_time
        success_rate = successes / urls_count if urls_count > 0 else 0
        urls_per_sec = urls_count / duration if duration > 0 else 0

        performance = {
            "batch_num": batch_num,
            "urls_count": urls_count,
            "successes": successes,
            "errors": errors,
            "success_rate": success_rate,
            "duration": duration,
            "urls_per_sec": urls_per_sec,
            "timestamp": datetime.now(),
        }
        self.performance_history.append(performance)
        return performance

    def update_progress_bar(
        self,
        pbar: tqdm,
        batch_num: int,
        total_batches: int,
        performance: Dict[str, Any],
        system_resources: Dict[str, float],
    ):
        """
        Update tqdm progress bar with batch info and metrics.
        """
        desc = f"Batch {batch_num}/{total_batches} | URLs/s: {performance['urls_per_sec']:.2f} | Success: {performance['success_rate']:.1%} | Mem: {system_resources['memory_percent']:.1f}% | CPU: {system_resources['cpu_percent']:.1f}%"
        pbar.set_description(desc)
        pbar.update(1)

    def retry_failed_urls(
        self, failed_urls: List[str], failure_type: str, max_retries: int = 3
    ) -> List[str]:
        """
        Retry failed URLs with exponential backoff.
        """
        retry_count = self.retry_counts.get(failure_type, 0)
        if retry_count >= max_retries:
            logger.warning(
                f"Max retries reached for {failure_type}: {len(failed_urls)} URLs skipped"
            )
            return []

        batch_size = max(
            5, len(failed_urls) // (2**retry_count)
        )  # Smaller batches on retry
        batches = self.split_into_batches(failed_urls, batch_size)

        backoff_time = (2**retry_count) + random.uniform(0, 1)
        logger.info(
            f"Retrying {len(failed_urls)} {failure_type} URLs in {len(batches)} batches, backoff: {backoff_time}s"
        )
        time.sleep(backoff_time)

        remaining_failed = []
        for batch in batches:
            try:
                results = self.engine.sync_batch_scrape(batch)
                for result in results:
                    if isinstance(result, Exception):
                        # Handle exception results from failed fetches
                        error_msg = str(result)
                        if "Failed to fetch" in error_msg:
                            url = error_msg.split("Failed to fetch ")[1].split(" ")[0]
                            remaining_failed.append(url)
                    elif isinstance(result, dict) and result.get("success", True):
                        try:
                            parsed_data = self.parser.parse(
                                result["html"], result["url"]
                            )
                            # Add required fields for database insertion
                            parsed_data["site_domain"] = urlparse(
                                parsed_data["url"]
                            ).netloc
                            parsed_data["scraped_at"] = datetime.now().isoformat()
                            pid = self.db_manager.insert_product(parsed_data)
                            if (
                                "variations" in parsed_data
                                and parsed_data["variations"]
                            ):
                                self.db_manager.insert_variations(
                                    pid,
                                    parsed_data["variations"],
                                    domain=parsed_data["site_domain"],
                                )
                        except Exception as e:
                            logger.error(f"Parse/DB error for {result['url']}: {e}")
                            remaining_failed.append(result["url"])
                    else:
                        # Handle failed dict results
                        if isinstance(result, dict) and "url" in result:
                            remaining_failed.append(result["url"])
            except Exception as e:
                logger.error(f"Retry batch failed: {e}")
                remaining_failed.extend(batch)

        self.retry_counts[failure_type] = retry_count + 1
        if remaining_failed:
            self.retry_queues[failure_type].extend(remaining_failed)
        return remaining_failed

    def fallback_to_sequential(self, urls: List[str], reason: str):
        """
        Graceful degradation to sequential processing.
        """
        logger.warning(f"Falling back to sequential processing due to: {reason}")
        results = []
        for url in tqdm(urls, desc="Sequential fallback"):
            try:
                result = self.engine.sync_scrape(url)
                parsed = self.parser.parse(result["html"], url)
                # Add required fields for database insertion
                parsed["site_domain"] = urlparse(parsed["url"]).netloc
                parsed["scraped_at"] = datetime.now().isoformat()
                self.db_manager.insert_product(parsed)
                results.append(result)
            except Exception as e:
                logger.error(f"Sequential scrape failed for {url}: {e}")
        logger.info(f"Sequential fallback completed: {len(results)} processed")
        return results

    def process_url_batches(
        self, urls: List[str], parser, db_manager, batch_size: Optional[int] = None
    ) -> Dict[str, Any]:
        """
        Main batch processing method.
        """
        self.parser = parser
        self.db_manager = db_manager

        if not urls:
            logger.info("No URLs to process")
            return {
                "scraped_products": 0,
                "variations": 0,
                "failed_urls": [],
                "batches_completed": 0,
            }

        system_resources = get_system_resources()
        optimal_batch_size = self.calculate_optimal_batch_size(
            len(urls), system_resources
        )
        actual_batch_size = batch_size if batch_size is not None else optimal_batch_size

        batches = self.split_into_batches(urls, actual_batch_size)
        total_batches = len(batches)

        all_failed = []
        overall_successes = 0
        overall_errors = 0
        total_variations = 0

        # Main progress bar for batches
        with tqdm(total=total_batches, desc="Processing batches", unit="batch") as pbar:
            for batch_num, batch in enumerate(batches, 1):
                start_time = time.time()
                batch_successes = 0
                batch_errors = 0
                batch_failed = []

                try:
                    # Concurrent scraping
                    results = self.engine.sync_batch_scrape(batch)
                    logger.debug(f"Batch {batch_num}: Scraped {len(results)} URLs")

                    # Sequential parsing and DB insertion
                    for result in results:
                        if isinstance(result, Exception):
                            # Handle exception results from failed fetches
                            batch_errors += 1
                            # Extract URL from exception message if possible
                            error_msg = str(result)
                            if "Failed to fetch" in error_msg:
                                url = error_msg.split("Failed to fetch ")[1].split(" ")[
                                    0
                                ]
                                batch_failed.append(url)
                                self.retry_queues["network_error"].append(url)
                        elif isinstance(result, dict) and result.get("success", True):
                            try:
                                parsed_data = parser.parse(
                                    result["html"], result["url"]
                                )
                                # Add required fields for database insertion
                                parsed_data["site_domain"] = urlparse(
                                    parsed_data["url"]
                                ).netloc
                                parsed_data["scraped_at"] = datetime.now().isoformat()
                                pid = db_manager.insert_product(parsed_data)
                                batch_successes += 1
                                if (
                                    "variations" in parsed_data
                                    and parsed_data["variations"]
                                ):
                                    var_ids = db_manager.insert_variations(
                                        pid,
                                        parsed_data["variations"],
                                        domain=parsed_data["site_domain"],
                                    )
                                    total_variations += len(var_ids)
                            except Exception as e:
                                logger.error(f"Parse/DB error for {result['url']}: {e}")
                                batch_errors += 1
                                batch_failed.append(result["url"])
                                self.retry_queues[
                                    "parse_error" if "parse" in str(e) else "db_error"
                                ].append(result["url"])
                        else:
                            # Handle failed dict results
                            batch_errors += 1
                            if isinstance(result, dict) and "url" in result:
                                batch_failed.append(result["url"])
                                self.retry_queues["network_error"].append(result["url"])

                    overall_successes += batch_successes
                    overall_errors += batch_errors
                    all_failed.extend(batch_failed)

                    # Track performance
                    performance = self.track_batch_performance(
                        batch_num, len(batch), batch_successes, batch_errors, start_time
                    )

                    # Update resources and progress
                    system_resources = get_system_resources()
                    self.update_progress_bar(
                        pbar, batch_num, total_batches, performance, system_resources
                    )

                    # Check for degradation
                    if (
                        performance["success_rate"] < 0.5
                        or system_resources["memory_percent"] > 95
                    ):
                        logger.warning("Performance degradation detected")
                        remaining_urls = batch_failed + [
                            u for b in batches[batch_num:] for u in b
                        ]
                        self.fallback_to_sequential(
                            remaining_urls, "high error rate or memory pressure"
                        )
                        break

                except Exception as e:
                    logger.error(f"Batch {batch_num} failed: {e}")
                    batch_errors = len(batch)
                    overall_errors += batch_errors
                    all_failed.extend(batch)
                    # Retry the entire batch
                    remaining = self.retry_failed_urls(batch, "batch_error")
                    all_failed.extend(remaining)

        # Final retry for all failed
        if all_failed:
            logger.info(f"Retrying {len(all_failed)} failed URLs")
            for failure_type, queue in self.retry_queues.items():
                if queue:
                    remaining = self.retry_failed_urls(queue, failure_type)
                    all_failed = [
                        u for u in all_failed if u not in queue or u in remaining
                    ]

        # Final stats
        total_processed = len(urls)
        final_success_rate = (
            overall_successes / total_processed if total_processed > 0 else 0
        )
        logger.info(
            f"Batch processing completed: {overall_successes}/{total_processed} successful ({final_success_rate:.1%})"
        )

        if final_success_rate < 0.7 and all_failed:
            logger.info(
                "Overall degradation: falling back remaining failed URLs to sequential"
            )
            self.fallback_to_sequential(all_failed, "overall low success rate")

        # Return results
        return {
            "scraped_products": overall_successes,
            "variations": total_variations,
            "failed_urls": all_failed,
            "batches_completed": total_batches,
        }
