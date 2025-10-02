import json
import os
import platform
import uuid
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, asdict
from pathlib import Path
import sys
import logging
from crontab import CronTab
from croniter import croniter
from database.manager import DatabaseManager


@dataclass
class ScheduledTask:
    """Represents a scheduled scraping task"""

    id: str
    task_name: str
    cron_schedule: str
    base_url: str
    email: str
    status: str = "active"
    created_at: Optional[datetime] = None
    last_run: Optional[datetime] = None
    next_run: Optional[datetime] = None
    retry_count: int = 0
    max_retries: int = 3
    execution_timeout: int = 3600  # seconds

    def __post_init__(self):
        if self.created_at is None:
            self.created_at = datetime.now()
        if self.next_run is None:
            self._calculate_next_run()

    def _calculate_next_run(self):
        """Calculate next run time based on cron schedule"""
        try:
            cron = croniter(self.cron_schedule, datetime.now())
            self.next_run = cron.get_next(datetime)
        except Exception as e:
            logging.error(f"Error calculating next run for task {self.id}: {e}")
            self.next_run = None

    def to_dict(self) -> Dict:
        """Convert to dictionary for JSON serialization"""
        data = asdict(self)
        # Convert datetime objects to ISO format strings
        for field in ["created_at", "last_run", "next_run"]:
            if data[field]:
                data[field] = data[field].isoformat()
        return data

    @classmethod
    def from_dict(cls, data: Dict) -> "ScheduledTask":
        """Create instance from dictionary"""
        # Convert ISO format strings back to datetime objects
        for field in ["created_at", "last_run", "next_run"]:
            if data.get(field):
                data[field] = datetime.fromisoformat(data[field])
        return cls(**data)


class CronJobManager:
    """Manages cron job creation and deletion"""

    def __init__(self, config: Dict):
        self.config = config
        self.cron_file = config.get("scheduling", {}).get(
            "cron_file", "/tmp/webscraper_cron"
        )
        self.logger = logging.getLogger(__name__)

        # Determine platform-specific cron handling
        self.is_windows = platform.system().lower() == "windows"
        if self.is_windows:
            self.logger.warning(
                "Windows detected - cron functionality limited. Consider using Windows Task Scheduler."
            )

    def create_cron_job(self, task: ScheduledTask) -> bool:
        """Create a cron job for the given task"""
        try:
            if self.is_windows:
                return self._create_windows_task(task)
            else:
                return self._create_unix_cron_job(task)
        except Exception as e:
            self.logger.error(f"Failed to create cron job for task {task.id}: {e}")
            return False

    def _create_unix_cron_job(self, task: ScheduledTask) -> bool:
        """Create Unix/Linux cron job"""
        try:
            # Get current user's crontab
            cron = CronTab(user=True)

            # Create command to run
            script_path = Path(__file__).resolve().parents[1] / "main.py"
            command = f"cd {script_path.parent} && {sys.executable} {script_path} --execute-scheduled-task {task.id} >> /tmp/webscraper_cron.log 2>&1"

            # Create new cron job
            job = cron.new(
                command=command,
                comment=f"WebScraper Task: {task.task_name} ({task.id})",
            )
            job.setall(task.cron_schedule)

            # Validate the schedule
            if not job.is_valid():
                self.logger.error(f"Invalid cron schedule: {task.cron_schedule}")
                return False

            # Write to crontab
            cron.write()
            self.logger.info(
                f"Created cron job for task {task.id}: {task.cron_schedule}"
            )
            return True

        except Exception as e:
            self.logger.error(f"Error creating Unix cron job: {e}")
            return False

    def _create_windows_task(self, task: ScheduledTask) -> bool:
        """Create Windows scheduled task (basic implementation)"""
        try:
            # Convert cron schedule to Windows task scheduler format
            # This is a simplified implementation
            script_path = Path(__file__).resolve().parents[1] / "main.py"

            # Create a basic batch file for the task
            bat_content = f"""@echo off
cd /d "{script_path.parent}"
{sys.executable} "{script_path}" --execute-scheduled-task {task.id} >> webscraper_task.log 2>&1
"""

            # Harden path handling by using absolute path for batch file
            bat_file = Path.cwd() / f"webscraper_task_{task.id}.bat"
            with open(bat_file, "w") as f:
                f.write(bat_content)

            self.logger.info(
                f"Created Windows batch file for task {task.id}: {bat_file}"
            )

            # Emit the exact schtasks command users should run
            schtasks_command = f'schtasks /Create /TN "WebScraper_{task.id}" /TR "cmd /c \\"{bat_file}\\"" /SC DAILY /ST 12:00 /F'
            self.logger.info(
                f"Example schtasks command (adjust schedule as needed): {schtasks_command}"
            )
            self.logger.warning(
                "Please manually configure Windows Task Scheduler to run this batch file using the above command template"
            )
            return True

        except Exception as e:
            self.logger.error(f"Error creating Windows task: {e}")
            return False

    def delete_cron_job(self, task_id: str) -> bool:
        """Delete cron job for the given task ID"""
        try:
            if self.is_windows:
                return self._delete_windows_task(task_id)
            else:
                return self._delete_unix_cron_job(task_id)
        except Exception as e:
            self.logger.error(f"Failed to delete cron job for task {task_id}: {e}")
            return False

    def _delete_unix_cron_job(self, task_id: str) -> bool:
        """Delete Unix/Linux cron job"""
        try:
            cron = CronTab(user=True)

            # Find and remove jobs with matching task ID
            jobs_removed = 0
            for job in cron:
                if task_id in job.comment:
                    cron.remove(job)
                    jobs_removed += 1

            if jobs_removed > 0:
                cron.write()
                self.logger.info(
                    f"Removed {jobs_removed} cron job(s) for task {task_id}"
                )
                return True
            else:
                self.logger.warning(f"No cron jobs found for task {task_id}")
                return False

        except Exception as e:
            self.logger.error(f"Error deleting Unix cron job: {e}")
            return False

    def _delete_windows_task(self, task_id: str) -> bool:
        """Delete Windows scheduled task"""
        try:
            bat_file = f"webscraper_task_{task_id}.bat"
            if os.path.exists(bat_file):
                os.remove(bat_file)
                self.logger.info(f"Removed Windows batch file for task {task_id}")
                return True
            else:
                self.logger.warning(f"No batch file found for task {task_id}")
                return False

        except Exception as e:
            self.logger.error(f"Error deleting Windows task: {e}")
            return False


class TaskExecutor:
    """Executes scheduled tasks with error handling and retry logic"""

    def __init__(self, config: Dict):
        self.config = config
        self.max_retries = config.get("scheduling", {}).get(
            "max_task_retries", config.get("scheduling", {}).get("max_retries", 3)
        )
        self.retry_delay_minutes = config.get("scheduling", {}).get(
            "retry_delay_minutes", 15
        )
        self.task_timeout = (
            config.get("scheduling", {}).get("task_timeout_minutes", 60) * 60
        )
        self.logger = logging.getLogger(__name__)

    def execute_task(self, task: ScheduledTask) -> Dict[str, Any]:
        """Execute a scheduled task with monitoring and error handling"""
        execution_start = datetime.now()
        result = {
            "task_id": task.id,
            "execution_start": execution_start,
            "execution_end": None,
            "status": "running",
            "products_scraped": 0,
            "variations_found": 0,
            "errors_count": 0,
            "error_details": None,
            "output": [],
        }

        try:
            self.logger.info(
                f"Starting execution of scheduled task: {task.task_name} ({task.id})"
            )

            # Import ScraperEngine here to avoid circular imports
            from core.scraper_engine import ScraperEngine

            # Initialize scraper engine
            scraper = ScraperEngine(self.config)

            # Execute scraping
            scrape_result = scraper.run_scheduled_scrape(task)

            # Update result with scraping data
            result.update(
                {
                    "status": scrape_result.get("status", "success"),
                    "products_scraped": scrape_result.get("scraped_products", 0),
                    "variations_found": scrape_result.get("variations", 0),
                    "output": scrape_result.get("output", []),
                }
            )

            # Update task's last run time
            task.last_run = execution_start
            task.next_run = self._calculate_next_run(task)
            task.retry_count = 0  # Reset retry count on success

            self.logger.info(f"Successfully completed task {task.id}")

        except Exception as e:
            self.logger.error(f"Error executing task {task.id}: {e}")
            result.update(
                {"status": "failed", "errors_count": 1, "error_details": str(e)}
            )

            # Handle retry logic
            if task.retry_count < self.max_retries:
                task.retry_count += 1
                task.next_run = datetime.now() + timedelta(
                    minutes=self.retry_delay_minutes * task.retry_count
                )  # Exponential backoff
                self.logger.info(
                    f"Scheduling retry {task.retry_count}/{self.max_retries} for task {task.id}"
                )
            else:
                task.status = "failed"
                self.logger.error(
                    f"Task {task.id} failed after {self.max_retries} retries"
                )

        finally:
            result["execution_end"] = datetime.now()
            result["duration_seconds"] = (
                result["execution_end"] - execution_start
            ).total_seconds()

        return result

    def _calculate_next_run(self, task: ScheduledTask) -> datetime:
        """Calculate next run time for the task"""
        try:
            cron = croniter(task.cron_schedule, datetime.now())
            return cron.get_next(datetime)
        except Exception as e:
            self.logger.error(f"Error calculating next run for task {task.id}: {e}")
            return datetime.now() + timedelta(days=1)  # Default to daily


class ScheduleManager:
    """Main scheduling orchestrator"""

    def __init__(self, config_path: str):
        self.config_path = config_path
        self.config = self._load_config()
        self.task_storage_path = self._get_task_storage_path()
        self.cron_manager = CronJobManager(self.config)
        self.task_executor = TaskExecutor(self.config)
        self.db_manager = DatabaseManager()
        self.logger = logging.getLogger(__name__)

        # Ensure data directory exists
        os.makedirs(os.path.dirname(self.task_storage_path), exist_ok=True)

        # Initialize database tables
        self.db_manager.init_db()

        # Migrate existing JSON data to database if needed
        self._migrate_json_to_db()

        # Check if JSON mirroring is enabled
        self.mirror_to_json = self.config.get("scheduling", {}).get(
            "mirror_to_json", True
        )

    def _load_config(self) -> Dict:
        """Load configuration from JSON file"""
        try:
            with open(self.config_path, "r") as f:
                return json.load(f)
        except Exception as e:
            self.logger.error(f"Error loading config: {e}")
            return {}

    def _get_task_storage_path(self) -> str:
        """Get path for task storage file"""
        return self.config.get("scheduling", {}).get(
            "task_storage", "data/scheduled_tasks.json"
        )

    def _migrate_json_to_db(self) -> None:
        """Migrate existing JSON data to database at startup"""
        try:
            # Check if database has any tasks
            existing_tasks = self.db_manager.execute_query(
                "SELECT COUNT(*) FROM scheduled_tasks"
            )
            if existing_tasks and existing_tasks[0][0] > 0:
                self.logger.info(
                    "Database already contains scheduled tasks, skipping migration"
                )
                return

            # Check if JSON file exists
            if not os.path.exists(self.task_storage_path):
                self.logger.info(
                    "No existing JSON file found, starting with empty database"
                )
                return

            # Load tasks from JSON
            with open(self.task_storage_path, "r") as f:
                tasks_data = json.load(f)

            if not tasks_data:
                self.logger.info("JSON file is empty, no migration needed")
                return

            # Convert and insert tasks into database
            migrated_count = 0
            for task_data in tasks_data:
                try:
                    # Ensure all required fields are present
                    if not all(
                        key in task_data
                        for key in [
                            "id",
                            "task_name",
                            "cron_schedule",
                            "base_url",
                            "email",
                        ]
                    ):
                        self.logger.warning(f"Skipping invalid task data: {task_data}")
                        continue

                    # Insert into database
                    self.db_manager.execute_query(
                        """
                        INSERT INTO scheduled_tasks
                        (id, task_name, cron_schedule, base_url, email, status, last_run, next_run, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                        [
                            task_data["id"],
                            task_data["task_name"],
                            task_data["cron_schedule"],
                            task_data["base_url"],
                            task_data["email"],
                            task_data.get("status", "active"),
                            task_data.get("last_run"),
                            task_data.get("next_run"),
                            task_data.get("created_at"),
                            task_data.get("updated_at", datetime.now().isoformat()),
                        ],
                    )
                    migrated_count += 1

                except Exception as e:
                    self.logger.error(
                        f"Failed to migrate task {task_data.get('id', 'unknown')}: {e}"
                    )
                    continue

            if migrated_count > 0:
                self.logger.info(
                    f"Successfully migrated {migrated_count} tasks from JSON to database"
                )
                # Optionally backup the original JSON file
                backup_path = f"{self.task_storage_path}.backup"
                import shutil

                shutil.copy2(self.task_storage_path, backup_path)
                self.logger.info(f"Original JSON file backed up to {backup_path}")

        except Exception as e:
            self.logger.error(f"Error during JSON to DB migration: {e}")

    def _load_tasks(self) -> List[ScheduledTask]:
        """Load scheduled tasks from database"""
        try:
            results = self.db_manager.execute_query(
                """
                SELECT id, task_name, cron_schedule, base_url, email, status,
                       last_run, next_run, created_at, updated_at
                FROM scheduled_tasks
                ORDER BY created_at
            """
            )

            tasks = []
            for row in results:
                task_data = {
                    "id": row[0],
                    "task_name": row[1],
                    "cron_schedule": row[2],
                    "base_url": row[3],
                    "email": row[4],
                    "status": row[5],
                    "last_run": row[6],
                    "next_run": row[7],
                    "created_at": row[8],
                    "updated_at": row[9],
                }
                tasks.append(ScheduledTask.from_dict(task_data))

            return tasks

        except Exception as e:
            self.logger.error(f"Error loading tasks from database: {e}")
            return []

    def _save_tasks(self, tasks: List[ScheduledTask]) -> bool:
        """Save scheduled tasks to database and optionally mirror to JSON"""
        try:
            # Save to database (primary storage)
            # First, clear existing tasks
            self.db_manager.execute_query("DELETE FROM scheduled_tasks")

            # Insert all tasks
            for task in tasks:
                self.db_manager.execute_query(
                    """
                    INSERT INTO scheduled_tasks
                    (id, task_name, cron_schedule, base_url, email, status, last_run, next_run, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                    [
                        task.id,
                        task.task_name,
                        task.cron_schedule,
                        task.base_url,
                        task.email,
                        task.status,
                        task.last_run.isoformat() if task.last_run else None,
                        task.next_run.isoformat() if task.next_run else None,
                        task.created_at.isoformat() if task.created_at else None,
                        datetime.now().isoformat(),
                    ],
                )

            # Optionally mirror to JSON for portability
            if self.mirror_to_json:
                tasks_data = [task.to_dict() for task in tasks]
                with open(self.task_storage_path, "w") as f:
                    json.dump(tasks_data, f, indent=2, default=str)

            return True

        except Exception as e:
            self.logger.error(f"Error saving tasks: {e}")
            return False

    def create_scheduled_task(
        self, schedule: str, name: str, url: str, email: str
    ) -> Optional[str]:
        """Create a new scheduled task"""
        try:
            # Validate cron expression
            if not self._validate_cron_expression(schedule):
                self.logger.error(f"Invalid cron expression: {schedule}")
                return None

            # Create new task
            task = ScheduledTask(
                id=str(uuid.uuid4()),
                task_name=name,
                cron_schedule=schedule,
                base_url=url,
                email=email,
            )

            # Create cron job
            if self.cron_manager.create_cron_job(task):
                # Save task to storage
                tasks = self._load_tasks()
                tasks.append(task)

                if self._save_tasks(tasks):
                    self.logger.info(
                        f"Created scheduled task: {task.task_name} ({task.id})"
                    )
                    return task.id
                else:
                    # If saving fails, try to remove the cron job
                    self.cron_manager.delete_cron_job(task.id)
                    return None
            else:
                return None

        except Exception as e:
            self.logger.error(f"Error creating scheduled task: {e}")
            return None

    def list_scheduled_tasks(self) -> List[Dict]:
        """List all scheduled tasks"""
        tasks = self._load_tasks()
        return [
            {
                "id": task.id,
                "name": task.task_name,
                "schedule": task.cron_schedule,
                "url": task.base_url,
                "email": task.email,
                "status": task.status,
                "created_at": task.created_at.isoformat() if task.created_at else None,
                "last_run": task.last_run.isoformat() if task.last_run else None,
                "next_run": task.next_run.isoformat() if task.next_run else None,
                "retry_count": task.retry_count,
            }
            for task in tasks
        ]

    def update_schedule(self, task_id: str, new_schedule: str) -> bool:
        """Update the schedule for an existing task"""
        try:
            if not self._validate_cron_expression(new_schedule):
                self.logger.error(f"Invalid cron expression: {new_schedule}")
                return False

            tasks = self._load_tasks()
            task_found = False

            for task in tasks:
                if task.id == task_id:
                    # Delete old cron job
                    self.cron_manager.delete_cron_job(task.id)

                    # Update schedule
                    task.cron_schedule = new_schedule
                    task._calculate_next_run()

                    # Create new cron job
                    if self.cron_manager.create_cron_job(task):
                        task_found = True
                        break
                    else:
                        return False

            if task_found:
                success = self._save_tasks(tasks)
                return success
            else:
                self.logger.error(f"Task not found: {task_id}")
                return False

        except Exception as e:
            self.logger.error(f"Error updating schedule: {e}")
            return False

    def delete_scheduled_task(self, task_id: str) -> bool:
        """Delete a scheduled task"""
        try:
            tasks = self._load_tasks()
            tasks_before = len(tasks)

            # Remove task from list
            tasks = [task for task in tasks if task.id != task_id]

            if len(tasks) < tasks_before:
                # Delete cron job
                self.cron_manager.delete_cron_job(task_id)

                # Save updated tasks
                if self._save_tasks(tasks):
                    self.logger.info(f"Deleted scheduled task: {task_id}")
                    return True
                else:
                    return False
            else:
                self.logger.error(f"Task not found: {task_id}")
                return False

        except Exception as e:
            self.logger.error(f"Error deleting scheduled task: {e}")
            return False

    def execute_scheduled_task(self, task_id: str) -> Dict[str, Any]:
        """Execute a specific scheduled task"""
        try:
            tasks = self._load_tasks()

            for task in tasks:
                if task.id == task_id:
                    result = self.task_executor.execute_task(task)

                    # Save updated task (with new last_run time)
                    self._save_tasks(tasks)

                    # Insert execution record into database
                    try:
                        execution_start = result.get("execution_start")
                        execution_end = result.get("execution_end")

                        self.db_manager.execute_query(
                            """
                            INSERT INTO task_executions
                            (task_id, execution_start, execution_end, status, products_scraped, variations_found, errors_count, error_details)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                            [
                                task_id,
                                (
                                    execution_start.isoformat()
                                    if isinstance(execution_start, datetime)
                                    else datetime.now().isoformat()
                                ),
                                (
                                    execution_end.isoformat()
                                    if isinstance(execution_end, datetime)
                                    else datetime.now().isoformat()
                                ),
                                result.get("status", "unknown"),
                                result.get("products_scraped", 0),
                                result.get("variations_found", 0),
                                result.get("errors_count", 0),
                                result.get("error_details"),
                            ],
                        )
                        self.logger.info(
                            f"Inserted task execution record into database for task: {task_id}"
                        )
                    except Exception as db_e:
                        self.logger.error(
                            f"Failed to insert task execution into database: {db_e}"
                        )
                        # Continue even if DB insert fails

                    return result

            raise ValueError(f"Task not found: {task_id}")

        except Exception as e:
            self.logger.error(f"Error executing scheduled task {task_id}: {e}")
            return {"task_id": task_id, "status": "error", "error_details": str(e)}

    def _validate_cron_expression(self, expression: str) -> bool:
        """Validate cron expression"""
        try:
            croniter(expression)
            return True
        except Exception:
            return False

    def cleanup_old_tasks(self) -> int:
        """Clean up old completed/failed tasks"""
        try:
            cleanup_days = self.config.get("scheduling", {}).get(
                "cleanup_old_tasks_days", 30
            )
            cutoff_date = datetime.now() - timedelta(days=cleanup_days)

            tasks = self._load_tasks()
            tasks_before = len(tasks)

            # Keep active tasks and recent tasks
            tasks = [
                task
                for task in tasks
                if task.status == "active"
                or (task.last_run and task.last_run > cutoff_date)
                or (task.created_at and task.created_at > cutoff_date)
            ]

            tasks_removed = tasks_before - len(tasks)

            if tasks_removed > 0:
                self._save_tasks(tasks)
                self.logger.info(f"Cleaned up {tasks_removed} old tasks")

            return tasks_removed

        except Exception as e:
            self.logger.error(f"Error during cleanup: {e}")
            return 0

    def get_task_statistics(self) -> Dict:
        """Get statistics about scheduled tasks"""
        try:
            tasks = self._load_tasks()

            stats = {
                "total_tasks": len(tasks),
                "active_tasks": len([t for t in tasks if t.status == "active"]),
                "failed_tasks": len([t for t in tasks if t.status == "failed"]),
                "tasks_with_retries": len([t for t in tasks if t.retry_count > 0]),
                "avg_retry_count": (
                    sum(t.retry_count for t in tasks) / len(tasks) if tasks else 0
                ),
                "next_scheduled_run": None,
            }

            # Find next scheduled run
            active_tasks = [t for t in tasks if t.status == "active" and t.next_run]
            if active_tasks:
                next_runs = [t.next_run for t in active_tasks if t.next_run]
                if next_runs:
                    next_run = min(next_runs)
                    stats["next_scheduled_run"] = next_run.isoformat()

            return stats

        except Exception as e:
            self.logger.error(f"Error getting task statistics: {e}")
            return {}
