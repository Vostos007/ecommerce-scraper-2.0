from __future__ import annotations

import importlib
from functools import lru_cache
from typing import Dict

from .interfaces import VariationParserProtocol


_REGISTRY: Dict[str, str] = {
    "default": "parsers.variation.impl.legacy",
}


@lru_cache(maxsize=None)
def _load_parser(path: str) -> VariationParserProtocol:
    module = importlib.import_module(path)
    parser_cls = getattr(module, "Parser")
    return parser_cls()


def get_parser(source: str | None = None) -> VariationParserProtocol:
    key = source or "default"
    if key not in _REGISTRY:
        key = "default"
    return _load_parser(_REGISTRY[key])
