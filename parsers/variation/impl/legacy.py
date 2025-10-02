from __future__ import annotations

from typing import Dict, List, Optional

from parsers.variation_parser import VariationParser


class Parser:
    """Адаптер, оборачивающий текущий VariationParser для нового API."""

    def extract_variations(
        self,
        html: Optional[str] = None,
        *,
        page=None,
        url: Optional[str] = None,
        antibot=None,
        cms_type: Optional[str] = None,
    ) -> List[Dict]:
        parser = VariationParser(
            antibot_manager=antibot,
            page=page,
            cms_type=cms_type,
        )
        return parser.extract_variations(html=html, page=page, url=url)
