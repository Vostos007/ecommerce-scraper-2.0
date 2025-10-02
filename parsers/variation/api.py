from __future__ import annotations

from typing import Optional

from .registry import get_parser


def get_optimal_scraping_method(url: str, html: Optional[str] = None) -> str:
    """Heuristic определения стратегии скрапинга.

    Пока логика простая: отдаём `"ajax"` для доменов sixwool/insales, иначе `"static"`.
    Функция нужна для совместимости с API уровня orchestration и будет расширена.
    """

    lowered = (url or "").lower()
    if any(domain in lowered for domain in ("sixwool", "insales", "shopify")):
        return "ajax"
    return "static"


def extract_variations(
    source: Optional[str] = None,
    *,
    html: Optional[str] = None,
    page=None,
    url: Optional[str] = None,
    antibot=None,
    cms_type: Optional[str] = None,
):
    parser = get_parser(source)
    return parser.extract_variations(
        html=html,
        page=page,
        url=url,
        antibot=antibot,
        cms_type=cms_type,
    )
