from __future__ import annotations

from typing import Protocol, List, Dict, Optional


class VariationParserProtocol(Protocol):
    def extract_variations(
        self,
        html: Optional[str] = None,
        *,
        page=None,
        url: Optional[str] = None,
        antibot=None,
        cms_type: Optional[str] = None,
    ) -> List[Dict]:
        ...


class ScrapingStrategy(Protocol):
    def __call__(self, url: str, html: Optional[str] = None) -> str:
        ...
