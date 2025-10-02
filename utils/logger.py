import logging
import os
import json
from datetime import UTC, datetime
from typing import Optional, Dict, Any
from colorama import Fore, Style, init

init(autoreset=True)

from tqdm import tqdm


def setup_logger(
    name="scraper", level=logging.INFO, log_file="data/logs/scrape.log", console=True
):
    """Setup logger with file and console handlers"""

    # Create logs directory if not exists
    os.makedirs(os.path.dirname(log_file), exist_ok=True)

    logger = logging.getLogger(name)
    logger.setLevel(level)

    # Avoid duplicate handlers
    if logger.hasHandlers():
        logger.handlers.clear()

    # File handler
    file_handler = logging.FileHandler(log_file)
    file_handler.setLevel(level)
    file_formatter = logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )
    file_handler.setFormatter(file_formatter)
    logger.addHandler(file_handler)

    # Console handler
    if console:
        console_handler = logging.StreamHandler()
        console_handler.setLevel(level)
        console_formatter = logging.Formatter(
            "%(asctime)s - %(levelname)s - %(message)s"
        )
        console_handler.setFormatter(console_formatter)
        logger.addHandler(console_handler)

    return logger


def colored_print(level: str, message: str, color: str = Fore.WHITE):
    """Print colored message to console"""
    colors = {
        "INFO": Fore.BLUE,
        "SUCCESS": Fore.GREEN,
        "WARNING": Fore.YELLOW,
        "ERROR": Fore.RED,
        "DEBUG": Fore.CYAN,
    }
    print(f"{colors.get(color, color) or colors.get(level, Fore.WHITE)}{message}")


def create_progress_bar(iterable, desc="Progress", unit="it"):
    """Create tqdm progress bar"""
    return tqdm(iterable, desc=desc, unit=unit, colour="green")


def log_scraping_step(step: str, url: Optional[str] = None, details: str = ""):
    """Log scraping steps with colors"""
    if url is not None:
        colored_print("INFO", f"Step: {step} - {url}", Fore.CYAN)
    else:
        colored_print("INFO", f"Step: {step}", Fore.CYAN)
    if details:
        print(details)


# ============================================================================
# ANTI-BOT SPECIFIC LOGGING ENHANCEMENTS
# ============================================================================


class StructuredFormatter(logging.Formatter):
    """JSON formatter for structured anti-bot logging"""

    def format(self, record):
        log_entry = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "event_type": getattr(record, "event_type", "general"),
            "event_data": getattr(record, "event_data", {}),
            "performance_metrics": getattr(record, "performance_metrics", {}),
        }

        # Add exception info if present
        if record.exc_info:
            log_entry["exception"] = self.formatException(record.exc_info)

        return json.dumps(log_entry, ensure_ascii=False)


class ColoredConsoleHandler(logging.StreamHandler):
    """Enhanced console handler with colors for anti-bot events"""

    pass


class ColoredFormatter(logging.Formatter):
    """Colored formatter for console output with anti-bot event highlighting"""

    COLORS = {
        "DEBUG": Fore.CYAN,
        "INFO": Fore.BLUE,
        "WARNING": Fore.YELLOW,
        "ERROR": Fore.RED,
        "CRITICAL": Fore.MAGENTA + Style.BRIGHT,
    }

    EVENT_COLORS = {
        "captcha": Fore.YELLOW + Style.BRIGHT,
        "user_agent": Fore.GREEN,
        "robots": Fore.CYAN,
        "antibot": Fore.MAGENTA,
        "proxy": Fore.BLUE,
        "delay": Fore.YELLOW,
        "general": Fore.WHITE,
    }

    def format(self, record):
        # Add colored level
        record.levelname_colored = (
            self.COLORS.get(record.levelname, Fore.WHITE)
            + record.levelname
            + Style.RESET_ALL
        )

        # Add colored event type
        event_type = getattr(record, "event_type", "general")
        record.event_type_colored = (
            self.EVENT_COLORS.get(event_type, Fore.WHITE)
            + event_type.upper()
            + Style.RESET_ALL
        )

        # Ensure event_type is available for formatting
        if not hasattr(record, "event_type"):
            record.event_type = "general"

        return super().format(record)


class DefaultEventMetadataFilter(logging.Filter):
    """Ensure log records contain event metadata expected by antibot formatters."""

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: D401 (doc inherited)
        if not hasattr(record, "event_type"):
            record.event_type = "general"
        if not hasattr(record, "event_data"):
            record.event_data = {}
        if not hasattr(record, "performance_metrics"):
            record.performance_metrics = {}
        return True


def setup_antibot_logger(
    name="antibot",
    level=logging.INFO,
    log_file="logs/antibot.log",
    structured_file="logs/antibot_structured.jsonl",
    console=True,
    include_metrics=True,
    config: Optional[Dict[str, Any]] = None,
):
    """Setup specialized anti-bot logger with structured output

    Args:
        name: Logger name
        level: Logging level
        log_file: Path to log file
        structured_file: Path to structured JSON log file
        console: Whether to enable console logging
        include_metrics: Whether to include performance metrics
        config: Optional configuration dict from settings.json
    """

    # Use config values if provided
    if config:
        log_destinations = config.get("log_destinations", {})

        # Override with config values
        file_config = log_destinations.get("file", {})
        if file_config.get("enabled", True) and file_config.get("path"):
            log_file = file_config["path"]

        console_config = log_destinations.get("console", {})
        console = console_config.get("enabled", console)

        json_config = log_destinations.get("json_file", {})
        if json_config.get("enabled", True) and json_config.get("path"):
            structured_file = json_config["path"]

        # Set level from config
        if config.get("log_level"):
            level_str = config["log_level"].upper()
            level = getattr(logging, level_str, logging.INFO)

    # Create logs directory if not exists
    os.makedirs(os.path.dirname(log_file), exist_ok=True)
    if structured_file:
        os.makedirs(os.path.dirname(structured_file), exist_ok=True)

    logger = logging.getLogger(name)
    logger.setLevel(level)

    # Clear existing handlers
    if logger.hasHandlers():
        logger.handlers.clear()

    # Ensure required metadata is present on every record
    logger.addFilter(DefaultEventMetadataFilter())

    # Standard file handler
    file_handler = logging.FileHandler(log_file)
    file_formatter = logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - [%(event_type)s] - %(message)s"
    )
    file_handler.setFormatter(file_formatter)
    logger.addHandler(file_handler)

    # Structured JSON handler
    if structured_file:
        json_handler = logging.FileHandler(structured_file)
        json_formatter = StructuredFormatter()
        json_handler.setFormatter(json_formatter)
        logger.addHandler(json_handler)

    # Enhanced console handler with colors
    if console:
        console_handler = ColoredConsoleHandler()
        console_formatter = ColoredFormatter(
            "%(asctime)s - %(levelname_colored)s - [%(event_type_colored)s] - %(message)s"
        )
        console_handler.setFormatter(console_formatter)
        logger.addHandler(console_handler)

    return logger


def get_antibot_logger(name: str) -> logging.Logger:
    """Get specialized anti-bot logger"""
    logger_name = f"antibot.{name}"
    logger = logging.getLogger(logger_name)

    # If logger doesn't have handlers, set it up
    if not logger.handlers:
        # Try to load configuration from settings.json
        config = None
        try:
            import json
            from pathlib import Path

            config_path = Path("config/settings.json")
            if config_path.exists():
                with open(config_path, "r") as f:
                    full_config = json.load(f)
                    config = full_config.get("antibot_logging", {})
        except Exception:
            # Fall back to default configuration if loading fails
            pass

        return setup_antibot_logger(logger_name, config=config)

    return logger


def log_antibot_event(event_type: str, event_data: Dict[str, Any], level: str = "INFO"):
    """Structured anti-bot logging"""
    logger = get_antibot_logger("events")

    # Create log record with custom attributes
    log_level = getattr(logging, level.upper(), logging.INFO)
    record = logger.makeRecord(
        logger.name, log_level, __file__, 0, f"Anti-bot event: {event_type}", (), None
    )

    # Add custom attributes
    record.event_type = event_type
    record.event_data = event_data
    record.performance_metrics = event_data.get("metrics", {})

    logger.handle(record)


def log_captcha_event(
    captcha_type: str, solve_time: float, success: bool, cost: float = None
):
    """CAPTCHA event logging"""
    event_data = {
        "captcha_type": captcha_type,
        "solve_time_seconds": solve_time,
        "success": success,
        "cost_usd": cost,
        "metrics": {"solve_time": solve_time, "success_rate": 1.0 if success else 0.0},
    }

    level = "INFO" if success else "WARNING"
    log_antibot_event("captcha", event_data, level)


def log_user_agent_rotation(old_ua: str, new_ua: str, trigger: str, domain: str = None):
    """User agent rotation logging"""
    event_data = {
        "old_user_agent": old_ua,
        "new_user_agent": new_ua,
        "rotation_trigger": trigger,
        "domain": domain,
        "timestamp": datetime.now(UTC).isoformat(),
    }

    log_antibot_event("user_agent", event_data, "INFO")


def log_robots_compliance(url: str, allowed: bool, crawl_delay: float, user_agent: str):
    """Robots.txt compliance logging"""
    event_data = {
        "url": url,
        "allowed": allowed,
        "crawl_delay_seconds": crawl_delay,
        "user_agent": user_agent,
        "compliance_status": "compliant" if allowed else "blocked",
        "metrics": {
            "crawl_delay": crawl_delay,
            "compliance_rate": 1.0 if allowed else 0.0,
        },
    }

    level = "INFO" if allowed else "WARNING"
    log_antibot_event("robots", event_data, level)


def get_logger(name: str) -> logging.Logger:
    """Get logger instance for the given name.

    This is a compatibility function for existing code that expects get_logger.
    It returns a standard logger or sets up antibot logger if name contains 'antibot'.
    """
    if "antibot" in name.lower():
        return get_antibot_logger(name)
    else:
        return setup_logger(name)
