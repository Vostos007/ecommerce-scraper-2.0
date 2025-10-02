"""Unified configuration loading utility to eliminate duplication across components."""

import json
import logging
import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Dict, Any, Optional, List


class ConfigurationError(Exception):
    """Configuration loading and validation errors."""
    pass


class ConfigLoader:
    """Centralized configuration loader with caching and validation."""
    
    def __init__(self):
        self.logger = logging.getLogger(__name__)
        self._config_cache: Dict[str, Dict[str, Any]] = {}
        self._missing_env_vars: set[str] = set()

    @lru_cache(maxsize=10)
    def load_config(self, config_path: str) -> Dict[str, Any]:
        """Load and cache JSON configuration with unified error handling.
        
        Args:
            config_path: Path to configuration file
            
        Returns:
            Configuration dictionary
            
        Raises:
            ConfigurationError: If file not found or JSON invalid
        """
        if config_path in self._config_cache:
            return self._config_cache[config_path]
            
        config_file = Path(config_path)
        if not config_file.exists():
            error_msg = f"Configuration file not found: {config_path}"
            self.logger.error(error_msg)
            raise ConfigurationError(error_msg)
            
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                config = json.load(f)
        except json.JSONDecodeError as e:
            error_msg = f"Invalid JSON in configuration {config_path}: {e}"
            self.logger.error(error_msg)
            raise ConfigurationError(error_msg) from e
        except IOError as e:
            error_msg = f"Error reading configuration {config_path}: {e}"
            self.logger.error(error_msg)
            raise ConfigurationError(error_msg) from e

        config = self._substitute_env_variables(config)

        for message in self.validate_config_structure(config):
            self.logger.warning("Configuration validation warning: %s", message)

        for key_path, present in self.validate_api_keys(config).items():
            if not present:
                self.logger.warning("Configuration missing API key for %s", key_path)

        self._config_cache[config_path] = config
        self.logger.debug(f"Configuration loaded successfully: {config_path}")
        return config
    
    def get_nested_value(self, config: Dict[str, Any], key_path: str, 
                        default: Any = None) -> Any:
        """Get nested configuration value using dot notation.
        
        Args:
            config: Configuration dictionary
            key_path: Dot-separated key path (e.g., 'database.timeout')
            default: Default value if key not found
            
        Returns:
            Configuration value or default
            
        Example:
            >>> config = {'database': {'timeout': 30, 'pool_size': 5}}
            >>> loader.get_nested_value(config, 'database.timeout')
            30
        """
        value = config
        for key in key_path.split('.'):
            if isinstance(value, dict) and key in value:
                value = value[key]
            else:
                return default
        return value
        
    def validate_required_keys(self, config: Dict[str, Any], 
                              required_keys: list[str]) -> None:
        """Validate that all required configuration keys are present.
        
        Args:
            config: Configuration dictionary to validate
            required_keys: List of required key paths (supports dot notation)
            
        Raises:
            ConfigurationError: If any required key is missing
        """
        missing_keys = []
        for key_path in required_keys:
            if self.get_nested_value(config, key_path) is None:
                missing_keys.append(key_path)
                
        if missing_keys:
            error_msg = f"Missing required configuration keys: {missing_keys}"
            self.logger.error(error_msg)
            raise ConfigurationError(error_msg)
    
    def get_component_config(self, config: Dict[str, Any], component_name: str,
                           default_enabled: bool = True) -> Dict[str, Any]:
        """Extract component-specific configuration with standard patterns.
        
        Args:
            config: Full configuration dictionary
            component_name: Name of component to extract config for
            default_enabled: Default value for 'enabled' flag
            
        Returns:
            Component configuration with standard keys
        """
        component_config = config.get(component_name, {})
        
        # Standard component configuration pattern
        return {
            "enabled": component_config.get("enabled", default_enabled),
            "timeout": component_config.get("timeout", 30.0),
            "max_retries": component_config.get("max_retries", 3),
            **component_config  # Merge in all original keys
        }
        
    def clear_cache(self, config_path: Optional[str] = None) -> None:
        """Clear configuration cache.
        
        Args:
            config_path: Specific config file to clear, or None for all
        """
        if config_path:
            self._config_cache.pop(config_path, None)
            self.load_config.cache_clear()  # Clear LRU cache for this specific path
        else:
            self._config_cache.clear()
            self.load_config.cache_clear()  # Clear entire LRU cache
            self._missing_env_vars.clear()
        self.logger.debug(f"Configuration cache cleared: {config_path or 'all'}")

    ENV_PATTERN = re.compile(r"\$\{([A-Z0-9_]+)\}")

    def _substitute_env_variables(self, value: Any) -> Any:
        if isinstance(value, dict):
            return {k: self._substitute_env_variables(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self._substitute_env_variables(item) for item in value]
        if isinstance(value, str):
            def replace(match: re.Match[str]) -> str:
                env_name = match.group(1)
                env_value = os.getenv(env_name)
                if env_value is None:
                    if env_name not in self._missing_env_vars:
                        self.logger.warning(
                            "Environment variable %s is not set; substituting empty string",
                            env_name,
                        )
                        self._missing_env_vars.add(env_name)
                    return ""
                return env_value

            return self.ENV_PATTERN.sub(replace, value)
        return value

    def validate_config_structure(self, config: Dict[str, Any]) -> List[str]:
        errors: List[str] = []
        required_sections: Dict[str, type] = {
            "scraper_backend": str,
            "proxy_infrastructure": dict,
            "captcha_solving": dict,
            "webhook_notifications": dict,
        }

        for key, expected_type in required_sections.items():
            value = config.get(key)
            if value is None:
                errors.append(f"Missing required configuration section '{key}'")
            elif expected_type is dict and not isinstance(value, dict):
                errors.append(f"Section '{key}' must be an object in configuration")
            elif expected_type is str and not isinstance(value, str):
                errors.append(f"Value '{key}' must be a string in configuration")

        return errors

    def validate_api_keys(self, config: Dict[str, Any]) -> Dict[str, bool]:
        status: Dict[str, bool] = {}

        proxy_root = config.get("proxy_infrastructure", {})
        if isinstance(proxy_root, dict):
            proxies = proxy_root.get("premium_proxies", {})
        else:
            proxies = {}
        if isinstance(proxies, dict):
            proxy6 = proxies.get("proxy6", {}) if isinstance(proxies.get("proxy6"), dict) else {}
            if proxies.get("enabled"):
                status["premium_proxies.proxy6.api_key"] = bool(proxy6.get("api_key"))
            backup_services = proxies.get("backup_services", {}) if isinstance(proxies.get("backup_services"), dict) else {}
            proxy_seller = backup_services.get("proxy_seller", {}) if isinstance(backup_services.get("proxy_seller"), dict) else {}
            if proxy_seller.get("enabled"):
                status["premium_proxies.backup_services.proxy_seller.api_key"] = bool(proxy_seller.get("api_key"))

        captcha = config.get("captcha_solving", {})
        if isinstance(captcha, dict) and captcha.get("enabled"):
            status["captcha_solving.api_key"] = bool(captcha.get("api_key"))

        webhook = config.get("webhook_notifications", {})
        if isinstance(webhook, dict) and webhook.get("enabled"):
            security = webhook.get("security", {}) if isinstance(webhook.get("security"), dict) else {}
            status["webhook_notifications.security.secret_key"] = bool(security.get("secret_key"))

        firecrawl = config.get("firecrawl", {})
        if isinstance(firecrawl, dict) and firecrawl.get("enabled"):
            status["firecrawl.api_key"] = bool(firecrawl.get("api_key"))

        api_credentials = config.get("api_credentials", {})
        if isinstance(api_credentials, dict):
            cscart = (
                api_credentials.get("cscart", {})
                if isinstance(api_credentials.get("cscart"), dict)
                else {}
            )
            if "api_key" in cscart:
                status["api_credentials.cscart.api_key"] = bool(cscart.get("api_key"))

            insales = (
                api_credentials.get("insales", {})
                if isinstance(api_credentials.get("insales"), dict)
                else {}
            )
            if "api_key" in insales:
                status["api_credentials.insales.api_key"] = bool(
                    insales.get("api_key")
                )

        return status


# Global instance for application-wide use
config_loader = ConfigLoader()


# Convenience functions for backward compatibility and ease of use
def load_config(config_path: str = "config/settings.json") -> Dict[str, Any]:
    """Load configuration using global config loader instance.
    
    Args:
        config_path: Path to configuration file
        
    Returns:
        Configuration dictionary
    """
    return config_loader.load_config(config_path)


def get_config_value(config: Dict[str, Any], key_path: str, default: Any = None) -> Any:
    """Get nested configuration value.
    
    Args:
        config: Configuration dictionary
        key_path: Dot-separated key path
        default: Default value if key not found
        
    Returns:
        Configuration value or default
    """
    return config_loader.get_nested_value(config, key_path, default)


def validate_config_structure(config: Dict[str, Any]) -> List[str]:
    """Validate configuration structure using the global loader instance."""

    return config_loader.validate_config_structure(config)


def validate_api_keys(config: Dict[str, Any]) -> Dict[str, bool]:
    """Validate API key presence using the global loader instance."""

    return config_loader.validate_api_keys(config)
