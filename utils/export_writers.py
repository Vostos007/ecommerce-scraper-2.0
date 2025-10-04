from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from importlib import import_module
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, TYPE_CHECKING
import importlib.util

if TYPE_CHECKING:  # pragma: no cover - typing only
    import pandas as pd

logger = logging.getLogger(__name__)

__all__ = [
    "write_product_exports",
    "apply_tabular_style",
    "ExportArtifacts",
    "CSV_SHEETS",
]

_PANDAS_AVAILABLE = importlib.util.find_spec("pandas") is not None
_pd_module = None

_OPENPYXL_PRIMITIVES: Dict[str, Any] | None = None
_OPENPYXL_STYLES: Dict[str, Any] | None = None

_PLACEHOLDER_NAME_PREFIX = "Sample Product "
_PLACEHOLDER_SCRAPED_AT_PREFIX = "2024-10-01T12:"

CSV_SHEETS: Dict[str, str] = {
    "full": "full.csv",
    "seo": "seo.csv",
    "diff": "diff.csv",
}

EXPORT_DISPLAY_TZ = ZoneInfo("Europe/Moscow")


FULL_CSV_COLUMNS: Tuple[str, ...] = (
    "url",
    "final_url",
    "http_status",
    "fetched_at",
    "title",
    "h1",
    "price",
    "stock",
    "stock_value",
    "currency",
    "availability",
    "sku",
    "brand",
    "category",
    "breadcrumbs",
    "images",
    "attrs_json",
    "text_hash",
    "variation_id",
    "variation_sku",
    "variation_type",
    "variation_value",
    "variation_price",
    "variation_stock",
    "variation_in_stock",
    "variation_attributes",
)

SEO_CSV_COLUMNS: Tuple[str, ...] = (
    "url",
    "fetched_at",
    "title",
    "meta_description",
    "h1",
    "og_title",
    "og_description",
    "og_image",
    "twitter_title",
    "twitter_description",
    "canonical",
    "robots",
    "hreflang",
    "images_alt_joined",
)

DIFF_CSV_COLUMNS: Tuple[str, ...] = (
    "url",
    "prev_crawl_at",
    "curr_crawl_at",
    "change_type",
    "fields_changed",
    "price_prev",
    "price_curr",
    "availability_prev",
    "availability_curr",
    "title_prev",
    "title_curr",
)


@dataclass(frozen=True)
class ExportArtifacts:
    json_path: Path
    csv_paths: Dict[str, Path]
    excel_path: Optional[Path]

    def csv_for(self, sheet: str) -> Path:
        try:
            return self.csv_paths[sheet]
        except KeyError as error:  # pragma: no cover - defensive guard for callers
            raise KeyError(f"Unknown CSV sheet '{sheet}'") from error


def _load_openpyxl_primitives() -> Dict[str, Any]:
    """Lazy-load openpyxl symbols only when exports actually need them."""

    global _OPENPYXL_PRIMITIVES
    if _OPENPYXL_PRIMITIVES is None:
        # Cold start profiling (ARCH-008) shows openpyxl dominating import time, so delay it.
        styles = import_module("openpyxl.styles")
        utils_module = import_module("openpyxl.utils")
        table_module = import_module("openpyxl.worksheet.table")
        _OPENPYXL_PRIMITIVES = {
            "Alignment": styles.Alignment,
            "Border": styles.Border,
            "Font": styles.Font,
            "PatternFill": styles.PatternFill,
            "Side": styles.Side,
            "get_column_letter": utils_module.get_column_letter,
            "Table": table_module.Table,
            "TableStyleInfo": table_module.TableStyleInfo,
        }
    return _OPENPYXL_PRIMITIVES


def _get_openpyxl_styles() -> Dict[str, Any]:
    global _OPENPYXL_STYLES
    if _OPENPYXL_STYLES is None:
        primitives = _load_openpyxl_primitives()
        Side = primitives["Side"]
        Border = primitives["Border"]
        PatternFill = primitives["PatternFill"]
        Font = primitives["Font"]
        Alignment = primitives["Alignment"]

        thin_side = Side(style="thin", color="D1D5DB")
        _OPENPYXL_STYLES = {
            "GRID_BORDER": Border(
                top=thin_side, bottom=thin_side, left=thin_side, right=thin_side
            ),
            "HEADER_FILL": PatternFill(fill_type="solid", fgColor="E5E7EB"),
            "HEADER_FONT": Font(name="Calibri", size=11, bold=True, color="1F2937"),
            "BODY_FONT": Font(name="Calibri", size=11, color="111827"),
            "ALIGN_LEFT": Alignment(
                vertical="center", horizontal="left", wrap_text=True
            ),
            "ALIGN_RIGHT": Alignment(vertical="center", horizontal="right"),
            "ALIGN_CENTER": Alignment(
                vertical="center", horizontal="center", wrap_text=True
            ),
        }
    return _OPENPYXL_STYLES


def _ensure_pandas():
    if not _PANDAS_AVAILABLE:
        raise RuntimeError(
            "pandas is required for export writers. Install optional dependency via `pip install pandas`."
        )

    global _pd_module
    if _pd_module is None:
        import pandas as _pd  # type: ignore import

        _pd_module = _pd
    return _pd_module


LARGE_DATASET_ROW_THRESHOLD = 150_000


def _sanitize_table_name(name: str) -> str:
    cleaned = [ch if ch.isalnum() else "_" for ch in name]
    candidate = "".join(cleaned).strip("_") or "table"
    return candidate[:31]


def apply_tabular_style(ws) -> None:
    """Apply consistent styling to an openpyxl worksheet."""

    primitives = _load_openpyxl_primitives()
    styles = _get_openpyxl_styles()
    get_column_letter = primitives["get_column_letter"]
    Table = primitives["Table"]
    TableStyleInfo = primitives["TableStyleInfo"]
    header_font = styles["HEADER_FONT"]
    header_fill = styles["HEADER_FILL"]
    grid_border = styles["GRID_BORDER"]
    align_center = styles["ALIGN_CENTER"]
    align_left = styles["ALIGN_LEFT"]
    align_right = styles["ALIGN_RIGHT"]

    if ws.max_row == 0 or ws.max_column == 0:
        return

    ws.freeze_panes = "A2"

    header_cells = next(ws.iter_rows(min_row=1, max_row=1))
    for cell in header_cells:
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = align_center
        cell.border = grid_border

    data_ref = f"A1:{get_column_letter(ws.max_column)}{ws.max_row}"

    if ws.max_row - 1 > LARGE_DATASET_ROW_THRESHOLD:
        try:
            table = Table(
                displayName=_sanitize_table_name(f"{ws.title}_table"), ref=data_ref
            )
            table.tableStyleInfo = TableStyleInfo(
                name="TableStyleMedium9",
                showFirstColumn=False,
                showLastColumn=False,
                showRowStripes=True,
                showColumnStripes=False,
            )
            existing_tables = {tbl.displayName for tbl in getattr(ws, "_tables", [])}
            if table.displayName not in existing_tables:
                ws.add_table(table)
        except ValueError:  # pragma: no cover - large range edge cases
            logger.debug("Failed to add table style for %s", ws.title)
        ws.auto_filter.ref = data_ref
        return

    for row in ws.iter_rows(min_row=2):
        for cell in row:
            cell.font = styles["BODY_FONT"]
            cell.border = grid_border
            value = cell.value
            if isinstance(value, (int, float)):
                cell.alignment = align_right
            else:
                cell.alignment = align_left


def _serialize_value(value: Any) -> Any:
    """Convert complex values to JSON strings for flat tabular export."""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return value


def _to_float(value: Any) -> Optional[float]:
    """Convert value to float where possible."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        cleaned = value.strip().replace(" ", "")
        if cleaned:
            try:
                return float(cleaned.replace(",", "."))
            except ValueError:
                return None
    return None


def _parse_timestamp(value: Any, *, pd_module) -> Optional["pd.Timestamp"]:
    """Parse value into a timezone-aware pandas Timestamp, if possible."""

    if value is None:
        return None

    try:
        timestamp = pd_module.to_datetime(value, utc=True, errors="coerce")
    except Exception:
        return None

    if timestamp is None or pd_module.isna(timestamp):
        return None

    # `to_datetime` may return a Series for certain inputs; normalize to scalar.
    if hasattr(timestamp, "__iter__") and not isinstance(timestamp, datetime):
        try:
            # pandas returns Series/Index for some array-like inputs
            if len(timestamp) == 0:  # type: ignore[arg-type]
                return None
            timestamp = timestamp.iloc[0]  # type: ignore[index]
        except Exception:
            return None

    return timestamp


def _format_timestamp_for_display(value: Any, *, pd_module) -> Optional[str]:
    """Format timestamp-like value into human friendly UTC string."""

    timestamp = _parse_timestamp(value, pd_module=pd_module)
    if timestamp is None:
        return None

    if hasattr(timestamp, "to_pydatetime"):
        dt = timestamp.to_pydatetime()
    else:
        dt = timestamp

    if not isinstance(dt, datetime):
        return None

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    local_dt = dt.astimezone(EXPORT_DISPLAY_TZ)
    return local_dt.strftime("%Y-%m-%d %H:%M:%S %Z")


def _extract_snapshot_timestamp(
    dataframe: Optional["pd.DataFrame"], *, pd_module
) -> Optional["pd.Timestamp"]:
    """Return the most recent scraped_at timestamp for the dataframe."""

    if dataframe is None or dataframe.empty or "scraped_at" not in dataframe.columns:
        return None

    try:
        timestamps = pd_module.to_datetime(
            dataframe["scraped_at"], utc=True, errors="coerce"
        )
    except Exception:
        return None

    if hasattr(timestamps, "dropna"):
        timestamps = timestamps.dropna()

    if getattr(timestamps, "empty", True):
        return None

    latest = timestamps.max()
    if latest is None or pd_module.isna(latest):
        return None
    return latest


def _format_elapsed_between_exports(
    current_ts: Optional["pd.Timestamp"],
    previous_ts: Optional["pd.Timestamp"],
) -> Optional[str]:
    """Return formatted elapsed time string in hours and days."""

    if current_ts is None or previous_ts is None:
        return None

    delta: timedelta = current_ts.to_pydatetime() - previous_ts.to_pydatetime()
    total_seconds = abs(delta.total_seconds())
    hours = total_seconds / 3600
    days = total_seconds / 86400

    # Use one decimal for hours, two decimals for days to aid quick scanning.
    return f"{hours:.1f} h / {days:.2f} d"


def _has_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, set, dict)):
        return bool(value)
    return True


def _first_value(source: Dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in source:
            value = source[key]
            if _has_value(value):
                return value
    return None


def _clean_str(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, str):
        cleaned = value.strip()
        return cleaned or None
    return str(value)


def _normalize_int(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        try:
            return int(value)
        except (TypeError, ValueError):  # pragma: no cover - defensive guard
            return None
    if isinstance(value, str):
        cleaned = value.strip()
        if not cleaned:
            return None
        try:
            return int(float(cleaned))
        except ValueError:
            return None
    return None


def _normalize_timestamp_field(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, str):
        cleaned = value.strip()
        return cleaned or None
    if isinstance(value, datetime):
        dt = value
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            dt = dt.astimezone(timezone.utc)
        iso_value = dt.isoformat()
        if iso_value.endswith("+00:00"):
            iso_value = iso_value[:-6] + "Z"
        return iso_value
    return None


def _choose_price(product: Dict[str, Any]) -> Any:
    direct_value = _first_value(product, "price", "base_price", "current_price")
    variations = product.get("variations")
    if isinstance(variations, list):
        for variation in variations:
            if not isinstance(variation, dict):
                continue
            candidate = _first_value(
                variation, "price", "variation_price", "current_price"
            )
            if not _has_value(candidate):
                continue
            numeric = _to_float(candidate)
            if numeric is not None and numeric > 0:
                return numeric
            return candidate
    return direct_value


def _normalize_price(value: Any) -> Optional[float]:
    if value is None:
        return None
    numeric = _to_float(value)
    if numeric is not None:
        return round(numeric, 2)
    if isinstance(value, str):
        cleaned = value.strip()
        if not cleaned:
            return None
    return None


def _normalize_stock(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, (list, dict)):
        return None
    numeric = _to_float(value)
    if numeric is not None:
        return round(numeric, 3)
    if isinstance(value, str):
        cleaned = value.strip()
        if not cleaned:
            return None
    return None


def _compute_stock_value(price_value: Any, stock_value: Any) -> Optional[float]:
    price_numeric = _to_float(price_value)
    stock_numeric = _to_float(stock_value)
    if price_numeric is None or stock_numeric is None:
        return None
    total = price_numeric * stock_numeric
    return round(total, 2)


def _normalize_availability(product: Dict[str, Any]) -> Optional[str]:
    availability = _first_value(
        product,
        "availability",
        "stock_status",
        "availability_status",
    )
    if isinstance(availability, str):
        cleaned = availability.strip()
        return cleaned or None

    in_stock_flag = product.get("in_stock")
    if isinstance(in_stock_flag, bool):
        return "in_stock" if in_stock_flag else "out_of_stock"

    stock_value = _first_value(product, "stock", "stock_quantity", "quantity")
    stock_numeric = _to_float(stock_value)
    if stock_numeric is not None:
        return "in_stock" if stock_numeric > 0 else "out_of_stock"

    variations = product.get("variations")
    if isinstance(variations, list) and variations:
        for variation in variations:
            if not isinstance(variation, dict):
                continue
            flag = variation.get("in_stock")
            if isinstance(flag, bool):
                if flag:
                    return "in_stock"
                continue
            v_stock = _first_value(variation, "stock", "stock_quantity")
            v_numeric = _to_float(v_stock)
            if v_numeric is not None and v_numeric > 0:
                return "in_stock"
        return "out_of_stock"

    return None


def _normalize_category(value: Any) -> Optional[str]:
    if isinstance(value, str):
        cleaned = value.strip()
        return cleaned or None
    if isinstance(value, dict):
        candidate = _first_value(value, "name", "title", "label", "category")
        return _clean_str(candidate)
    if isinstance(value, (list, tuple, set)):
        parts: List[str] = []
        for item in value:
            if isinstance(item, str):
                cleaned = item.strip()
                if cleaned:
                    parts.append(cleaned)
            elif isinstance(item, dict):
                label = _first_value(item, "name", "title", "label")
                if isinstance(label, str):
                    cleaned = label.strip()
                    if cleaned:
                        parts.append(cleaned)
        if parts:
            return ">".join(parts)
    return None


def _normalize_breadcrumbs(value: Any) -> Optional[str]:
    if isinstance(value, str):
        cleaned = value.strip()
        return cleaned or None
    if isinstance(value, (list, tuple)):
        parts: List[str] = []
        for item in value:
            if isinstance(item, str):
                cleaned = item.strip()
                if cleaned:
                    parts.append(cleaned)
            elif isinstance(item, dict):
                label = _first_value(item, "label", "name", "title", "text")
                if isinstance(label, str):
                    cleaned = label.strip()
                    if cleaned:
                        parts.append(cleaned)
        if parts:
            return ">".join(parts)
    return None


def _normalize_images(value: Any) -> Optional[str]:
    if isinstance(value, str):
        cleaned = value.strip()
        return cleaned or None

    urls: List[str] = []
    if isinstance(value, (list, tuple, set)):
        for item in value:
            candidate: Optional[str] = None
            if isinstance(item, str):
                candidate = item.strip()
            elif isinstance(item, dict):
                candidate = _first_value(item, "url", "src", "href")
                if isinstance(candidate, str):
                    candidate = candidate.strip()
                else:
                    candidate = None
            if candidate:
                urls.append(candidate)
    if urls:
        return "|".join(urls)
    return None


def _normalize_attrs_payload(product: Dict[str, Any]) -> Optional[str]:
    raw_attrs = product.get("attrs_json")
    if isinstance(raw_attrs, str):
        cleaned = raw_attrs.strip()
        return cleaned or None

    aggregated: Dict[str, Any] = {}
    if isinstance(raw_attrs, (dict, list)) and raw_attrs:
        aggregated["attrs_json"] = raw_attrs

    for key in (
        "attributes",
        "attrs",
        "characteristics",
        "properties",
        "specs",
        "specifications",
        "details",
    ):
        value = product.get(key)
        if isinstance(value, (dict, list)) and value:
            aggregated[key] = value

    variations = product.get("variations")
    if isinstance(variations, list) and variations:
        aggregated.setdefault("variations", variations)

    if not aggregated:
        return None

    try:
        return json.dumps(aggregated, ensure_ascii=False)
    except (TypeError, ValueError):  # pragma: no cover - defensive guard
        return None


def _normalize_hreflang(value: Any) -> Optional[str]:
    if isinstance(value, str):
        cleaned = value.strip()
        return cleaned or None

    entries: List[str] = []
    if isinstance(value, (list, tuple, set)):
        for item in value:
            if isinstance(item, str):
                cleaned = item.strip()
                if cleaned:
                    entries.append(cleaned)
            elif isinstance(item, dict):
                lang = _clean_str(_first_value(item, "lang", "locale"))
                href = _clean_str(_first_value(item, "url", "href"))
                if lang and href:
                    entries.append(f"{lang}|{href}")
                elif lang:
                    entries.append(lang)
                elif href:
                    entries.append(href)
    if entries:
        return "|".join(entries)
    return None


def _normalize_images_alt(value: Any) -> Optional[str]:
    if isinstance(value, str):
        cleaned = value.strip()
        return cleaned or None
    if isinstance(value, (list, tuple, set)):
        alts: List[str] = []
        for item in value:
            if isinstance(item, str):
                cleaned = item.strip()
                if cleaned:
                    alts.append(cleaned)
            elif isinstance(item, dict):
                text = _first_value(item, "alt", "text", "label")
                if isinstance(text, str):
                    cleaned = text.strip()
                    if cleaned:
                        alts.append(cleaned)
        if alts:
            return "|".join(alts)
    return None


def _build_full_rows(products: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for product in products:
        if not isinstance(product, dict):
            continue

        url = _clean_str(product.get("url"))
        if not url:
            continue

        price_candidate = _choose_price(product)
        stock_candidate = _first_value(
            product,
            "stock",
            "stock_quantity",
            "quantity",
            "inventory",
            "available",
        )
        normalized_price = _normalize_price(price_candidate)
        normalized_stock = _normalize_stock(stock_candidate)

        base_row: Dict[str, Any] = {
            "url": url,
            "final_url": _clean_str(
                _first_value(product, "final_url", "resolved_url", "canonical_url")
            ),
            "http_status": _normalize_int(
                _first_value(product, "http_status", "status_code")
            ),
            "fetched_at": _normalize_timestamp_field(
                _first_value(
                    product,
                    "fetched_at",
                    "scraped_at",
                    "collected_at",
                    "timestamp",
                )
            ),
            "title": _clean_str(_first_value(product, "title", "name")),
            "h1": _clean_str(_first_value(product, "h1", "seo_h1")),
            "price": normalized_price,
            "stock": normalized_stock,
            "stock_value": _compute_stock_value(normalized_price, normalized_stock),
            "currency": _clean_str(
                _first_value(product, "currency", "price_currency", "currency_code")
            ),
            "availability": _normalize_availability(product),
            "sku": _clean_str(
                _first_value(product, "sku", "article", "product_id", "id")
            ),
            "brand": _clean_str(_first_value(product, "brand", "manufacturer")),
            "category": _normalize_category(
                _first_value(product, "category", "categories")
            ),
            "breadcrumbs": _normalize_breadcrumbs(
                _first_value(product, "breadcrumbs", "breadcrumb", "breadcrumb_path")
            ),
            "images": _normalize_images(
                _first_value(product, "images", "image_urls", "gallery")
            ),
            "attrs_json": _normalize_attrs_payload(product),
            "text_hash": _clean_str(
                _first_value(product, "text_hash", "content_hash", "body_hash")
            ),
        }

        variations = product.get("variations")
        variation_rows_added = False
        if isinstance(variations, list) and variations:
            for variation in variations:
                if not isinstance(variation, dict):
                    continue

                variation_id = _clean_str(
                    variation.get("variation_id") or variation.get("variant_id")
                )
                variation_value = _clean_str(
                    variation.get("variation_value") or variation.get("value")
                )
                variation_type = _clean_str(
                    variation.get("variation_type") or variation.get("type")
                )
                variation_price = _first_value(
                    variation,
                    "variation_price",
                    "price",
                    "current_price",
                )
                variation_stock = _first_value(
                    variation,
                    "variation_stock",
                    "stock_quantity",
                    "stock",
                    "quantity",
                )
                variation_attributes = variation.get("variation_attributes") or variation.get(
                    "attributes"
                )

                row = dict(base_row)
                variation_price_normalized = _normalize_price(variation_price)
                variation_stock_normalized = _normalize_stock(variation_stock)

                if variation_price_normalized is not None:
                    row["price"] = variation_price_normalized
                if variation_stock_normalized is not None:
                    row["stock"] = variation_stock_normalized
                row["stock_value"] = _compute_stock_value(
                    row["price"],
                    row["stock"],
                )
                row["variation_id"] = variation_id
                row["variation_sku"] = _clean_str(
                    variation.get("variation_sku") or variation.get("sku")
                )
                row["variation_type"] = variation_type
                row["variation_value"] = variation_value
                row["variation_price"] = variation_price_normalized
                row["variation_stock"] = variation_stock_normalized
                variation_in_stock_flag = variation.get("variation_in_stock")
                if variation_in_stock_flag is None:
                    variation_in_stock_flag = variation.get("in_stock")
                row["variation_in_stock"] = bool(variation_in_stock_flag)
                row["variation_attributes"] = (
                    json.dumps(variation_attributes, ensure_ascii=False)
                    if isinstance(variation_attributes, (dict, list))
                    else _clean_str(variation_attributes)
                )
                rows.append(row)
                variation_rows_added = True

        if not variation_rows_added:
            row = dict(base_row)
            row.update(
                {
                    "variation_id": None,
                    "variation_sku": None,
                    "variation_type": None,
                    "variation_value": None,
                    "variation_price": None,
                    "variation_stock": None,
                    "variation_in_stock": None,
                    "variation_attributes": None,
                }
            )
            rows.append(row)
    return rows


def _build_full_dataframe(products: List[Dict[str, Any]]) -> "pd.DataFrame":
    pd = _ensure_pandas()
    rows = _build_full_rows(products)
    if not rows:
        return pd.DataFrame(columns=FULL_CSV_COLUMNS)

    dataframe = pd.DataFrame(rows)
    for column in FULL_CSV_COLUMNS:
        if column not in dataframe.columns:
            dataframe[column] = None
    dataframe = dataframe[list(FULL_CSV_COLUMNS)]
    return dataframe


def _build_seo_rows(products: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    seen_urls: set[str] = set()

    for product in products:
        if not isinstance(product, dict):
            continue

        url = _clean_str(product.get("url"))
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)

        fetched_at = _normalize_timestamp_field(
            _first_value(product, "fetched_at", "scraped_at", "collected_at", "timestamp")
        )

        og_block: Dict[str, Any] = {}
        for key in ("open_graph", "og", "og_data"):
            candidate = product.get(key)
            if isinstance(candidate, dict):
                og_block = candidate
                break

        twitter_block: Dict[str, Any] = {}
        for key in ("twitter", "twitter_card", "twitter_data"):
            candidate = product.get(key)
            if isinstance(candidate, dict):
                twitter_block = candidate
                break

        hreflang_source = (
            product.get("hreflang")
            or product.get("alternate_locales")
            or product.get("alternates")
            or product.get("alternate_hreflang")
        )

        images_alt_source = (
            product.get("images_alt")
            or product.get("image_alts")
            or product.get("alt_texts")
        )

        row: Dict[str, Any] = {
            "url": url,
            "fetched_at": fetched_at,
            "title": _clean_str(
                _first_value(product, "seo_title", "title", "name")
            ),
            "meta_description": _clean_str(
                _first_value(product, "seo_meta_description", "meta_description")
            ),
            "h1": _clean_str(_first_value(product, "seo_h1", "h1")),
            "og_title": _clean_str(
                _first_value(product, "og_title")
                or _first_value(og_block, "title", "og:title")
            ),
            "og_description": _clean_str(
                _first_value(product, "og_description")
                or _first_value(og_block, "description", "og:description")
            ),
            "og_image": _clean_str(
                _first_value(product, "og_image")
                or _first_value(og_block, "image", "og:image")
            ),
            "twitter_title": _clean_str(
                _first_value(product, "twitter_title")
                or _first_value(twitter_block, "title", "twitter:title")
            ),
            "twitter_description": _clean_str(
                _first_value(product, "twitter_description")
                or _first_value(twitter_block, "description", "twitter:description")
            ),
            "canonical": _clean_str(
                _first_value(product, "canonical", "canonical_url")
            ),
            "robots": _clean_str(
                _first_value(product, "robots", "meta_robots")
            ),
            "hreflang": _normalize_hreflang(hreflang_source),
            "images_alt_joined": _normalize_images_alt(images_alt_source),
        }
        rows.append(row)

    return rows


def _build_seo_dataframe(products: List[Dict[str, Any]]) -> "pd.DataFrame":
    pd = _ensure_pandas()
    rows = _build_seo_rows(products)
    if not rows:
        return pd.DataFrame(columns=SEO_CSV_COLUMNS)

    dataframe = pd.DataFrame(rows)
    for column in SEO_CSV_COLUMNS:
        if column not in dataframe.columns:
            dataframe[column] = None
    dataframe = dataframe[list(SEO_CSV_COLUMNS)]
    return dataframe


def _dataframe_records(
    dataframe: Optional["pd.DataFrame"], *, pd_module
) -> Dict[str, Dict[str, Any]]:
    if dataframe is None or dataframe.empty:
        return {}

    records = dataframe.to_dict(orient="records")
    result: Dict[str, Dict[str, Any]] = {}
    for record in records:
        cleaned: Dict[str, Any] = {}
        for key, value in record.items():
            cleaned[key] = None if pd_module.isna(value) else value
        url = cleaned.get("url")
        if isinstance(url, str) and url:
            result[url] = cleaned
    return result


def _build_diff_dataframe(
    new_df: "pd.DataFrame",
    previous_df: Optional["pd.DataFrame"],
) -> "pd.DataFrame":
    pd = _ensure_pandas()

    current_records = _dataframe_records(new_df, pd_module=pd)
    previous_records = _dataframe_records(previous_df, pd_module=pd)

    urls = sorted(set(current_records) | set(previous_records))
    if not urls:
        return pd.DataFrame(columns=DIFF_CSV_COLUMNS)

    diff_rows: List[Dict[str, Any]] = []

    for url in urls:
        current = current_records.get(url)
        previous = previous_records.get(url)

        if not current and not previous:
            continue

        if current and not previous:
            change_type = "ADDED"
            fields_changed = "added"
            diff_rows.append(
                {
                    "url": url,
                    "prev_crawl_at": None,
                    "curr_crawl_at": current.get("fetched_at"),
                    "change_type": change_type,
                    "fields_changed": fields_changed,
                    "price_prev": None,
                    "price_curr": current.get("price"),
                    "availability_prev": None,
                    "availability_curr": current.get("availability"),
                    "title_prev": None,
                    "title_curr": current.get("title"),
                }
            )
            continue

        if previous and not current:
            change_type = "REMOVED"
            fields_changed = "removed"
            diff_rows.append(
                {
                    "url": url,
                    "prev_crawl_at": previous.get("fetched_at"),
                    "curr_crawl_at": None,
                    "change_type": change_type,
                    "fields_changed": fields_changed,
                    "price_prev": previous.get("price"),
                    "price_curr": None,
                    "availability_prev": previous.get("availability"),
                    "availability_curr": None,
                    "title_prev": previous.get("title"),
                    "title_curr": None,
                }
            )
            continue

        if not current or not previous:
            continue

        fields_changed_list: List[str] = []

        price_prev_value = previous.get("price")
        price_curr_value = current.get("price")
        if _to_float(price_prev_value) != _to_float(price_curr_value):
            fields_changed_list.append("price")

        availability_prev = previous.get("availability")
        availability_curr = current.get("availability")
        if (availability_prev or availability_curr) and (
            availability_prev != availability_curr
        ):
            fields_changed_list.append("availability")

        title_prev = previous.get("title")
        title_curr = current.get("title")
        if (title_prev or title_curr) and title_prev != title_curr:
            fields_changed_list.append("title")

        text_hash_prev = previous.get("text_hash")
        text_hash_curr = current.get("text_hash")
        if (text_hash_prev or text_hash_curr) and text_hash_prev != text_hash_curr:
            fields_changed_list.append("text_hash")

        if not fields_changed_list:
            continue

        change_type = "MODIFIED"
        diff_rows.append(
            {
                "url": url,
                "prev_crawl_at": previous.get("fetched_at"),
                "curr_crawl_at": current.get("fetched_at"),
                "change_type": change_type,
                "fields_changed": ";".join(fields_changed_list),
                "price_prev": price_prev_value,
                "price_curr": price_curr_value,
                "availability_prev": availability_prev,
                "availability_curr": availability_curr,
                "title_prev": title_prev,
                "title_curr": title_curr,
            }
        )

    if not diff_rows:
        return pd.DataFrame(columns=DIFF_CSV_COLUMNS)

    dataframe = pd.DataFrame(diff_rows)
    for column in DIFF_CSV_COLUMNS:
        if column not in dataframe.columns:
            dataframe[column] = None
    dataframe = dataframe[list(DIFF_CSV_COLUMNS)]
    return dataframe


def _write_csv_exports(
    *,
    full_display: "pd.DataFrame",
    seo_dataframe: "pd.DataFrame",
    diff_dataframe: "pd.DataFrame",
    base_dir: Path,
) -> Dict[str, Path]:
    csv_paths: Dict[str, Path] = {}

    exports = {
        "full": full_display,
        "seo": seo_dataframe,
        "diff": diff_dataframe,
    }

    for sheet_name, frame in exports.items():
        file_name = CSV_SHEETS[sheet_name]
        target_path = base_dir / file_name
        frame.to_csv(
            target_path,
            index=False,
            sep=';',
            decimal=',',
            encoding='utf-8',
            lineterminator='\n',
        )
        csv_paths[sheet_name] = target_path

    manifest_path = base_dir / "export_manifest.json"
    manifest_payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "files": {sheet: path.name for sheet, path in csv_paths.items()},
    }
    manifest_path.write_text(
        json.dumps(manifest_payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    return csv_paths


def _is_placeholder_dataset(products: List[Dict[str, Any]]) -> bool:
    """Detect the synthetic payload emitted by legacy tests."""

    total = 0
    sample_names = 0
    placeholder_scrapes = 0
    for product in products:
        if not isinstance(product, dict):
            continue
        total += 1
        name = product.get("name")
        if isinstance(name, str) and name.startswith(_PLACEHOLDER_NAME_PREFIX):
            sample_names += 1
        scraped_at = product.get("scraped_at") or product.get("fetched_at")
        if isinstance(scraped_at, str) and scraped_at.startswith(
            _PLACEHOLDER_SCRAPED_AT_PREFIX
        ):
            placeholder_scrapes += 1

    if total == 0:
        return False

    ratio_names = sample_names / total
    ratio_scrapes = placeholder_scrapes / total
    return ratio_names >= 0.6 and ratio_scrapes >= 0.6


def _is_export_path_under_repo_sites(json_path: Path) -> bool:
    repo_site_root = (Path.cwd() / "data" / "sites").resolve()
    try:
        return json_path.resolve().is_relative_to(repo_site_root)
    except AttributeError:  # pragma: no cover - Python < 3.9 fallback
        return str(json_path.resolve()).startswith(str(repo_site_root))


def _extract_products_from_payload(payload: Any) -> List[Dict[str, Any]]:
    if isinstance(payload, dict):
        candidates = payload.get("products")
        if isinstance(candidates, list):
            return [item for item in candidates if isinstance(item, dict)]
        return []
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    return []


def write_product_exports(
    products: List[Dict[str, Any]],
    json_path: Path,
) -> ExportArtifacts:
    """Persist product payload, CSV файлы и (при необходимости) Excel."""

    if _is_export_path_under_repo_sites(json_path) and _is_placeholder_dataset(products):
        raise ValueError(
            f"Refusing to persist placeholder dataset to {json_path}. "
            "Ensure tests run against isolated directories."
        )

    json_path.parent.mkdir(parents=True, exist_ok=True)
    previous_dataframe: Optional["pd.DataFrame"] = None
    previous_products: List[Dict[str, Any]] = []
    if _PANDAS_AVAILABLE and json_path.exists():
        try:
            payload = json.loads(json_path.read_text(encoding="utf-8"))
            previous_products = _extract_products_from_payload(payload)
            if previous_products:
                previous_dataframe = _build_full_dataframe(previous_products)
        except Exception:  # pragma: no cover - defensive guard
            previous_dataframe = None

    generated_at = datetime.now(timezone.utc).isoformat()
    json_payload_obj = {
        "generated_at": generated_at,
        "products": products,
    }
    json_payload = json.dumps(json_payload_obj, ensure_ascii=False, indent=2)
    json_path.write_text(json_payload, encoding="utf-8")

    latest_json = json_path.parent / "latest.json"
    if latest_json != json_path:
        try:
            latest_json.write_text(json_payload, encoding="utf-8")
        except OSError:
            logger.debug("Failed to mirror export JSON to %s", latest_json)

    if not _PANDAS_AVAILABLE:
        logger.warning(
            "pandas is not available; skipping tabular exports for %s", json_path
        )
        return ExportArtifacts(json_path=json_path, csv_paths={}, excel_path=None)

    try:
        pd = _ensure_pandas()
        full_dataframe = _build_full_dataframe(products)
    except Exception as exc:  # pragma: no cover - defensive logging
        logger.warning("Failed to prepare tabular data for %s: %s", json_path, exc)
        return ExportArtifacts(json_path=json_path, csv_paths={}, excel_path=None)

    full_display = full_dataframe.copy()
    seo_dataframe = _build_seo_dataframe(products)
    diff_dataframe = _build_diff_dataframe(full_dataframe, previous_dataframe)

    csv_paths: Dict[str, Path] = {}
    try:
        csv_paths = _write_csv_exports(
            full_display=full_display,
            seo_dataframe=seo_dataframe,
            diff_dataframe=diff_dataframe,
            base_dir=json_path.parent,
        )
    except Exception as exc:  # pragma: no cover - CSV generation should rarely fail
        logger.warning("Failed to write CSV exports for %s: %s", json_path, exc)

    excel_path: Optional[Path] = None
    try:
        try:
            site_slug = json_path.parents[1].name
        except IndexError:
            site_slug = json_path.stem
        excel_path = json_path.with_suffix(".xlsx")
        alternate_excel = json_path.parent / f"{site_slug}_latest.xlsx"
        with pd.ExcelWriter(excel_path, engine="openpyxl") as writer:
            full_display.to_excel(writer, sheet_name="full", index=False)
            seo_dataframe.to_excel(writer, sheet_name="seo", index=False)
            diff_dataframe.to_excel(writer, sheet_name="diff", index=False)

            workbook = writer.book
            for sheet_name in writer.sheets:
                ws = workbook[sheet_name]
                apply_tabular_style(ws)

        excel_bytes = excel_path.read_bytes()
        if alternate_excel != excel_path:
            try:
                alternate_excel.write_bytes(excel_bytes)
            except OSError:
                logger.debug("Failed to mirror export to %s", alternate_excel)
        latest_excel = json_path.parent / "latest.xlsx"
        if latest_excel != excel_path:
            try:
                latest_excel.write_bytes(excel_bytes)
            except OSError:
                logger.debug("Failed to mirror export to %s", latest_excel)
    except Exception as exc:  # pragma: no cover - defensive logging of rare failures
        logger.warning("Failed to write Excel export for %s: %s", json_path, exc)
        excel_path = None

    return ExportArtifacts(json_path=json_path, csv_paths=csv_paths, excel_path=excel_path)
