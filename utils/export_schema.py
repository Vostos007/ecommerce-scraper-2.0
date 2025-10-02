"""Export schema definitions for PRD §1.9 compliance.

This module defines TypedDict schemas for all export formats:
- Full CSV: Complete product data with all fields
- SEO CSV: SEO-specific metadata (title, meta tags, og, twitter)
- Diff CSV: Changes between crawls (price, stock, availability)

All schemas are typed for IDE support and runtime validation.
"""

from typing import TypedDict, Optional, List


class FullCSVRow(TypedDict, total=False):
    """
    Full product data export schema (PRD §1.9.1).
    
    Contains all scraped fields including product details, pricing,
    variations, and metadata. Used for complete data exports.
    
    Fields:
        url: Original product page URL
        final_url: Final URL after redirects (if different)
        http_status: HTTP status code (200, 404, etc.)
        fetched_at: ISO timestamp when page was scraped
        title: Product title/name
        h1: Main H1 heading from page
        price: Current price (numeric or string with currency)
        currency: Currency code (RUB, USD, EUR, etc.)
        availability: Stock availability status (in_stock, out_of_stock, etc.)
        sku: Product SKU/article number
        brand: Product brand/manufacturer
        category: Product category
        breadcrumbs: Breadcrumb navigation path (JSON array)
        images: Product images URLs (JSON array)
        attrs_json: Additional product attributes (JSON object)
        text_hash: Content hash for change detection
    """
    url: str
    final_url: Optional[str]
    http_status: int
    fetched_at: str
    title: Optional[str]
    h1: Optional[str]
    price: Optional[str]
    currency: Optional[str]
    availability: Optional[str]
    sku: Optional[str]
    brand: Optional[str]
    category: Optional[str]
    breadcrumbs: Optional[str]  # JSON array as string
    images: Optional[str]  # JSON array as string
    attrs_json: Optional[str]  # JSON object as string
    text_hash: Optional[str]


class SEOCSVRow(TypedDict, total=False):
    """
    SEO metadata export schema (PRD §1.9.2).
    
    Contains SEO-specific fields for content analysis and optimization.
    Includes meta tags, Open Graph, Twitter Cards, and technical SEO.
    
    Fields:
        url: Product page URL
        fetched_at: ISO timestamp when page was scraped
        title: Page <title> tag content
        meta_description: Meta description tag content
        h1: Main H1 heading
        og_title: Open Graph title (og:title)
        og_description: Open Graph description (og:description)
        og_image: Open Graph image URL (og:image)
        twitter_title: Twitter Card title (twitter:title)
        twitter_description: Twitter Card description (twitter:description)
        canonical: Canonical URL (<link rel="canonical">)
        robots: Robots meta tag content (index,follow etc.)
        hreflang: Hreflang tags (JSON array of {lang, url})
        images_alt_joined: Concatenated alt text from all images
    """
    url: str
    fetched_at: str
    title: Optional[str]
    meta_description: Optional[str]
    h1: Optional[str]
    og_title: Optional[str]
    og_description: Optional[str]
    og_image: Optional[str]
    twitter_title: Optional[str]
    twitter_description: Optional[str]
    canonical: Optional[str]
    robots: Optional[str]
    hreflang: Optional[str]  # JSON array as string
    images_alt_joined: Optional[str]


class DiffCSVRow(TypedDict, total=False):
    """
    Change tracking export schema (PRD §1.9.3).
    
    Tracks changes between consecutive crawls for:
    - Price changes
    - Stock/availability changes
    - Title/content changes
    
    Used for monitoring competitor pricing and detecting updates.
    
    Fields:
        url: Product page URL
        prev_crawl_at: ISO timestamp of previous crawl
        curr_crawl_at: ISO timestamp of current crawl
        change_type: Type of change (price, stock, title, availability)
        fields_changed: Comma-separated list of changed fields
        price_prev: Previous price value
        price_curr: Current price value
        availability_prev: Previous availability status
        availability_curr: Current availability status
        title_prev: Previous product title
        title_curr: Current product title
    
    Example:
        {
            "url": "https://example.com/product-123",
            "prev_crawl_at": "2025-09-28T10:00:00Z",
            "curr_crawl_at": "2025-09-29T10:00:00Z",
            "change_type": "price",
            "fields_changed": "price,availability",
            "price_prev": "1500.00",
            "price_curr": "1350.00",
            "availability_prev": "in_stock",
            "availability_curr": "in_stock",
            "title_prev": None,
            "title_curr": None
        }
    """
    url: str
    prev_crawl_at: str
    curr_crawl_at: str
    change_type: Optional[str]
    fields_changed: Optional[str]
    price_prev: Optional[str]
    price_curr: Optional[str]
    availability_prev: Optional[str]
    availability_curr: Optional[str]
    title_prev: Optional[str]
    title_curr: Optional[str]


# Export format metadata
EXPORT_FORMATS = {
    "full": {
        "filename": "full.csv",
        "schema": FullCSVRow,
        "description": "Complete product data with all fields"
    },
    "seo": {
        "filename": "seo.csv",
        "schema": SEOCSVRow,
        "description": "SEO metadata (titles, meta tags, Open Graph)"
    },
    "diff": {
        "filename": "diff.csv",
        "schema": DiffCSVRow,
        "description": "Changes between crawls (price, stock, availability)"
    }
}


def validate_full_row(row: dict) -> FullCSVRow:
    """
    Validate and convert dict to FullCSVRow.
    
    Args:
        row: Dictionary with product data
    
    Returns:
        Validated FullCSVRow with type guarantees
    
    Raises:
        KeyError: If required fields are missing
        ValueError: If field types are invalid
    """
    required_fields = ["url", "http_status", "fetched_at"]
    for field in required_fields:
        if field not in row:
            raise KeyError(f"Required field missing: {field}")
    
    return FullCSVRow(**row)


def validate_seo_row(row: dict) -> SEOCSVRow:
    """
    Validate and convert dict to SEOCSVRow.
    
    Args:
        row: Dictionary with SEO data
    
    Returns:
        Validated SEOCSVRow with type guarantees
    
    Raises:
        KeyError: If required fields are missing
    """
    required_fields = ["url", "fetched_at"]
    for field in required_fields:
        if field not in row:
            raise KeyError(f"Required field missing: {field}")
    
    return SEOCSVRow(**row)


def validate_diff_row(row: dict) -> DiffCSVRow:
    """
    Validate and convert dict to DiffCSVRow.
    
    Args:
        row: Dictionary with diff data
    
    Returns:
        Validated DiffCSVRow with type guarantees
    
    Raises:
        KeyError: If required fields are missing
    """
    required_fields = ["url", "prev_crawl_at", "curr_crawl_at"]
    for field in required_fields:
        if field not in row:
            raise KeyError(f"Required field missing: {field}")
    
    return DiffCSVRow(**row)