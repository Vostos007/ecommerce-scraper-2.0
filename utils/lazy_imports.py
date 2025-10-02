"""Helpers for lazily importing heavy optional dependencies."""

from __future__ import annotations

import importlib
from types import ModuleType
from typing import Callable


class LazyModule:
    """Proxy that defers importing a module until an attribute is accessed."""

    def __init__(self, loader: Callable[[], ModuleType]) -> None:
        self._loader = loader
        self._module: ModuleType | None = None

    def _load(self) -> ModuleType:
        if self._module is None:
            self._module = self._loader()
        return self._module

    def __getattr__(self, item: str):  # pragma: no cover - thin proxy
        module = self._load()
        return getattr(module, item)

    def __dir__(self):  # pragma: no cover - introspection helper
        return dir(self._load())


def lazy_import(module_name: str) -> LazyModule:
    """Return a proxy that imports ``module_name`` on first use."""

    return LazyModule(lambda: importlib.import_module(module_name))
