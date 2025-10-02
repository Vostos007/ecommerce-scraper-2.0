"""Rich theming utilities for consistent display styling."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

from rich.console import Console
from rich.theme import Theme

_SETTINGS_CACHE: Dict[Path, Dict[str, Any]] = {}


def _load_settings(path: Path) -> Dict[str, Any]:
    if path in _SETTINGS_CACHE:
        return _SETTINGS_CACHE[path]
    if not path.exists():
        _SETTINGS_CACHE[path] = {}
        return {}
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    _SETTINGS_CACHE[path] = data
    return data


@dataclass
class RichConsoleOptions:
    """Console configuration options used across the CLI surface."""

    highlight: bool = True
    markup: bool = True
    soft_wrap: bool = False
    log_path: Optional[str] = None
    record: bool = False
    width: Optional[int] = None
    color_system: Optional[str] = None
    force_terminal: Optional[bool] = None
    stderr: bool = False

    @classmethod
    def from_config(cls, data: Dict[str, Any]) -> "RichConsoleOptions":
        options = data.get("console", {}) if data else {}
        return cls(
            highlight=options.get("highlight", True),
            markup=options.get("markup", True),
            soft_wrap=options.get("soft_wrap", False),
            log_path=options.get("log_path"),
            record=options.get("record", False),
            width=options.get("width"),
            color_system=options.get("color_system"),
            force_terminal=options.get("force_terminal"),
            stderr=options.get("stderr", False),
        )


@dataclass
class RichThemeConfig:
    """Encapsulates palette and style configuration for Rich components."""

    name: str = "default"
    variant: str = "dark"
    palette: Dict[str, str] = field(
        default_factory=lambda: {
            "success": "green",
            "error": "bold red",
            "warning": "bold yellow",
            "info": "cyan",
            "muted": "grey62",
            "accent": "magenta",
            "progress.text": "bold cyan",
            "progress.percentage": "bold green",
            "table.header": "bold cyan",
            "table.footer": "bold magenta",
            "table.success": "green",
            "table.error": "red",
            "table.neutral": "white",
            "tree.product": "bold white",
            "tree.variation": "cyan",
            "tree.meta": "dim",
        }
    )
    accessibility: Dict[str, Any] = field(
        default_factory=lambda: {
            "high_contrast": False,
            "color_blind_safe": False,
            "monochrome": False,
        }
    )
    progress: Dict[str, Any] = field(
        default_factory=lambda: {
            "bar_style": "green",
            "completed_style": "bold green",
            "pulse_style": "cyan",
            "failure_style": "bold red",
        }
    )
    tables: Dict[str, Any] = field(
        default_factory=lambda: {
            "box": "ROUNDED",
            "row_styles": ["", "dim"],
            "title_style": "bold magenta",
        }
    )
    console: RichConsoleOptions = field(default_factory=RichConsoleOptions)

    @classmethod
    def from_settings(cls, data: Dict[str, Any]) -> "RichThemeConfig":
        theme_data = data.get("theme", {}) if data else {}
        palette = theme_data.get("palette") or None
        accessibility = theme_data.get("accessibility") or None
        progress = theme_data.get("progress") or None
        tables = theme_data.get("tables") or None
        options = RichConsoleOptions.from_config(data)
        defaults = cls()
        config = cls(
            name=theme_data.get("name", defaults.name),
            variant=theme_data.get("variant", defaults.variant),
            palette=palette or dict(defaults.palette),
            accessibility=accessibility or dict(defaults.accessibility),
            progress=progress or dict(defaults.progress),
            tables=tables or dict(defaults.tables),
            console=options,
        )
        if config.accessibility.get("monochrome"):
            # Override palette with monochrome variants
            config.palette = {key: "white" for key in config.palette}
            config.progress.update({
                "bar_style": "white",
                "completed_style": "bold white",
                "pulse_style": "white",
            })
        if config.accessibility.get("high_contrast"):
            config.palette.setdefault("accent", "bold white on black")
            config.palette.setdefault("muted", "grey42")
        return config

    def to_theme(self) -> Theme:
        styles = {key: value for key, value in self.palette.items() if value}
        styles.update({
            "progress.description": self.palette.get("progress.text", "bold cyan"),
            "progress.percentage": self.palette.get("progress.percentage", "bold green"),
        })
        return Theme(styles)


class RichThemeManager:
    """Centralised theme management including load/save and console factory."""

    def __init__(self, settings_path: Path = Path("config/settings.json")) -> None:
        self.settings_path = settings_path
        self._settings: Dict[str, Any] = {}
        self._theme_section: Dict[str, Any] = {}
        self._theme_config: Optional[RichThemeConfig] = None
        self.reload()

    def reload(self) -> None:
        data = _load_settings(self.settings_path)
        self._settings = data
        self._theme_section = data.get("rich_display", {})
        self._theme_config = None

    @property
    def theme_config(self) -> RichThemeConfig:
        if self._theme_config is None:
            self._theme_config = RichThemeConfig.from_settings(self._theme_section)
        return self._theme_config

    def get_console(self, *, stderr: Optional[bool] = None, record: Optional[bool] = None) -> Console:
        config = self.theme_config
        options = config.console
        use_stderr = stderr if stderr is not None else options.stderr
        console = Console(
            highlight=options.highlight,
            markup=options.markup,
            soft_wrap=options.soft_wrap,
            record=record if record is not None else options.record,
            log_path=options.log_path,
            width=options.width,
            color_system=options.color_system,
            force_terminal=options.force_terminal,
            stderr=use_stderr,
            theme=config.to_theme(),
        )
        return console

    def available_variants(self) -> Iterable[str]:
        variants = self._theme_section.get("variants", [])
        if not variants:
            return (self.theme_config.variant,)
        return {variant.get("name", "default") for variant in variants}

    def switch_theme(self, name: str) -> None:
        variants = self._theme_section.get("variants", [])
        for variant in variants:
            if variant.get("name") == name:
                self._theme_section["theme"] = variant
                _SETTINGS_CACHE[self.settings_path]["rich_display"] = self._theme_section
                self._theme_config = RichThemeConfig.from_settings(self._theme_section)
                return
        raise ValueError(f"Theme variant '{name}' not found")

    def export_theme(self, path: Path) -> None:
        theme = {
            "theme": {
                "name": self.theme_config.name,
                "variant": self.theme_config.variant,
                "palette": self.theme_config.palette,
                "accessibility": self.theme_config.accessibility,
                "progress": self.theme_config.progress,
                "tables": self.theme_config.tables,
            }
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as handle:
            json.dump(theme, handle, ensure_ascii=False, indent=2)

    def import_theme(self, path: Path) -> None:
        if not path.exists():
            raise FileNotFoundError(path)
        with path.open("r", encoding="utf-8") as handle:
            theme_section = json.load(handle)
        rich_display = self._settings.setdefault("rich_display", {})
        rich_display.setdefault("variants", [])
        rich_display["variants"].append(theme_section.get("theme", {}))
        _SETTINGS_CACHE[self.settings_path] = self._settings
        self.reload()


def get_theme_manager(settings_path: Path = Path("config/settings.json")) -> RichThemeManager:
    return RichThemeManager(settings_path=settings_path)


def get_console(*, settings_path: Path = Path("config/settings.json"), stderr: bool = False, record: bool = False) -> Console:
    manager = get_theme_manager(settings_path)
    return manager.get_console(stderr=stderr, record=record)


__all__ = [
    "RichConsoleOptions",
    "RichThemeConfig",
    "RichThemeManager",
    "get_console",
    "get_theme_manager",
]
