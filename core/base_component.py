"""Base component class for eliminating common code duplication patterns."""

import json
import logging
import sqlite3
from abc import ABC
from typing import Dict, Any, Optional


class BaseComponent(ABC):
    """Base class with common initialization patterns to eliminate duplication."""

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        """Initialize base component with common patterns."""
        self.config = config or {}
        self.logger = self._setup_logger()

    def _setup_logger(self, name: Optional[str] = None) -> logging.Logger:
        """Setup logger with standardized configuration."""
        return logging.getLogger(name or self.__class__.__module__)

    @staticmethod
    def load_config(config_path: str) -> Dict[str, Any]:
        """Load JSON configuration with standardized error handling.

        Args:
            config_path: Path to JSON configuration file

        Returns:
            Dictionary containing configuration

        Raises:
            FileNotFoundError: If config file doesn't exist
            json.JSONDecodeError: If JSON is malformed
        """
        logger = logging.getLogger(__name__)
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            logger.error(f"Config file not found: {config_path}")
            raise
        except json.JSONDecodeError as e:
            logger.error(f"Invalid JSON in config {config_path}: {e}")
            raise

    def get_config_value(self, key: str, default: Any = None) -> Any:
        """Get configuration value with default fallback.

        Args:
            key: Configuration key (supports dot notation like 'database.timeout')
            default: Default value if key not found

        Returns:
            Configuration value or default
        """
        value = self.config
        for part in key.split("."):
            if isinstance(value, dict) and part in value:
                value = value[part]
            else:
                return default
        return value

    def is_enabled(self, feature_key: str = "enabled", default: bool = True) -> bool:
        """Check if feature is enabled in configuration.

        Args:
            feature_key: Configuration key to check
            default: Default value if key not found

        Returns:
            True if feature is enabled
        """
        return self.get_config_value(feature_key, default)


class BaseDatabaseComponent(BaseComponent):
    """Base class for components that interact with SQLite database."""

    def __init__(
        self, config: Optional[Dict[str, Any]] = None, db_path: Optional[str] = None
    ):
        super().__init__(config)
        self.db_path = db_path or self.get_config_value(
            "database.path", "data/database/competitor.db"
        )
        self.timeout = self.get_config_value("database.timeout", 30.0)

    def get_db_connection(self, enable_wal: bool = True) -> sqlite3.Connection:
        """Get standardized database connection with common PRAGMA settings.

        Args:
            enable_wal: Enable WAL journal mode for better performance

        Returns:
            Configured SQLite connection
        """
        conn = sqlite3.connect(self.db_path, timeout=self.timeout)
        conn.execute("PRAGMA foreign_keys = ON")

        if enable_wal:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = NORMAL")

        return conn

    def execute_with_error_handling(
        self, query: str, params: tuple = (), operation_name: str = "database operation"
    ) -> Any:
        """Execute database query with standardized error handling.

        Args:
            query: SQL query to execute
            params: Query parameters
            operation_name: Name of operation for logging

        Returns:
            Query result or None if error

        Raises:
            sqlite3.Error: Database operation errors
        """
        try:
            with self.get_db_connection() as conn:
                cursor = conn.execute(query, params)
                return cursor.fetchall()
        except sqlite3.Error as e:
            self.logger.error(f"Failed {operation_name}: {e}")
            raise


class ConfigurableComponent(BaseComponent):
    """Base class for components with complex configuration patterns."""

    def __init__(
        self, config_path: Optional[str] = None, config: Optional[Dict[str, Any]] = None
    ):
        """Initialize with either config path or direct config dict.

        Args:
            config_path: Path to JSON config file
            config: Direct configuration dictionary
        """
        if config_path and not config:
            config = self.load_config(config_path)
        elif not config:
            config = {}

        super().__init__(config)
        self.config_path = config_path
