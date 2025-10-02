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
    "full_data": "full_data.csv",
    "seo": "seo.csv",
    "changes": "changes.csv",
}

EXPORT_DISPLAY_TZ = ZoneInfo("Europe/Moscow")


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


def _flatten_products(products: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Flatten products and their variations into export-ready rows."""

    flattened: List[Dict[str, Any]] = []
    row_sequence = 0
    for product in products:
        if not isinstance(product, dict):
            continue

        base_url = product.get("url") or ""
        name = product.get("name") or ""
        scraped_at = product.get("scraped_at")
        seo_h1 = product.get("seo_h1")
        seo_title = product.get("seo_title")
        seo_meta_description = product.get("seo_meta_description")

        def _make_row() -> Dict[str, Any]:
            row: Dict[str, Any] = {
                "url": base_url,
                "name": name,
                "price": _serialize_value(product.get("price")),
                "base_price": _serialize_value(product.get("base_price")),
                "scraped_at": scraped_at,
                "seo_h1": seo_h1,
                "seo_title": seo_title,
                "seo_meta_description": seo_meta_description,
            }
            for key, value in product.items():
                if key in {"variations"} or key in row:
                    continue
                row[key] = _serialize_value(value)
            return row

        variations = product.get("variations")
        parent_total_stock: float = 0.0
        parent_total_value: float = 0.0

        variation_rows: List[Dict[str, Any]] = []
        if isinstance(variations, list) and variations:
            for variation in variations:
                if not isinstance(variation, dict):
                    continue
                row = _make_row()
                row["row_type"] = "variation"
                row["variation_value"] = variation.get("value")
                row["variation_type"] = variation.get("type")
                row["variation_price"] = variation.get("price")
                row["variation_stock"] = variation.get("stock")
                row["variation_url"] = variation.get("url") or variation.get("link")

                v_stock = _to_float(variation.get("stock"))
                v_price = _to_float(variation.get("price")) or _to_float(
                    product.get("price")
                )
                if v_stock is not None:
                    parent_total_stock += v_stock
                if v_stock is not None and v_price is not None:
                    total_value = v_stock * v_price
                    parent_total_value += total_value
                    row["total"] = round(total_value, 2)
                else:
                    row["total"] = None

                row["variation_sku"] = variation.get("sku")
                row["variation_variant_id"] = variation.get("variant_id")
                row["variation_attributes"] = _serialize_value(
                    variation.get("attributes")
                )
                row["entity_id"] = (
                    variation.get("url")
                    or f"variation::{base_url}::{variation.get('value')}"
                )
                row["_row_sequence"] = row_sequence
                row_sequence += 1
                variation_rows.append(row)

        # Build parent row (aggregated)
        parent_row = _make_row()
        parent_row["row_type"] = "parent"
        parent_row["variation_count"] = len(variation_rows)
        parent_row["_row_sequence"] = row_sequence
        row_sequence += 1

        if parent_total_stock:
            parent_row["stock"] = round(parent_total_stock, 2)
        else:
            parent_row["stock"] = product.get("stock")

        if parent_total_value:
            parent_row["total"] = round(parent_total_value, 2)
        else:
            base_stock = _to_float(product.get("stock"))
            base_price = _to_float(product.get("price"))
            if base_stock is not None and base_price is not None:
                parent_row["total"] = round(base_stock * base_price, 2)
            else:
                parent_row["total"] = None

        parent_row["variation_value"] = None
        parent_row["variation_type"] = None
        parent_row["variation_price"] = None
        parent_row["variation_stock"] = None
        parent_row["variation_url"] = None
        parent_row["variation_sku"] = None
        parent_row["variation_variant_id"] = None
        parent_row["variation_attributes"] = None
        parent_row["entity_id"] = f"parent::{base_url}"

        flattened.append(parent_row)
        flattened.extend(variation_rows)

    return flattened


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
        scraped_at = product.get("scraped_at")
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


def _build_full_dataframe(products: List[Dict[str, Any]]) -> "pd.DataFrame":
    pd = _ensure_pandas()
    rows = _flatten_products(products)
    dataframe = pd.DataFrame(rows)

    if "_row_sequence" in dataframe.columns:
        try:
            dataframe = dataframe.sort_values(by=["_row_sequence"], kind="stable")
        except Exception:
            pass
        dataframe = dataframe.drop(columns=["_row_sequence"], errors="ignore")

    desired_columns = [
        "entity_id",
        "row_type",
        "url",
        "variation_url",
        "name",
        "variation_value",
        "variation_type",
        "price",
        "variation_price",
        "base_price",
        "stock",
        "variation_stock",
        "total",
        "scraped_at",
        "seo_h1",
        "seo_title",
        "seo_meta_description",
    ]

    for column in desired_columns:
        if column not in dataframe.columns:
            dataframe[column] = None

    extra_columns = [col for col in dataframe.columns if col not in desired_columns]
    dataframe = dataframe[desired_columns + sorted(extra_columns)]
    return dataframe


def _build_seo_dataframe(full_df: "pd.DataFrame") -> "pd.DataFrame":
    seo_df = full_df[full_df["row_type"] == "parent"].copy()
    return seo_df[["url", "name", "seo_h1", "seo_title", "seo_meta_description"]]


def _build_diff_dataframe(
    new_df: "pd.DataFrame",
    previous_df: Optional["pd.DataFrame"],
) -> "pd.DataFrame":
    pd = _ensure_pandas()
    columns = [
        "entity_id",
        "row_type",
        "url",
        "variation_url",
        "name",
        "variation_value",
        "old_price",
        "new_price",
        "price_delta",
        "old_stock",
        "new_stock",
        "stock_delta",
        "previous_scraped_at",
        "current_scraped_at",
        "Прошедшее время между выгрузками (в часах и днях)",
    ]

    if previous_df is None or previous_df.empty:
        return pd.DataFrame(columns=columns)

    previous_snapshot_ts = _extract_snapshot_timestamp(
        previous_df, pd_module=pd
    )
    current_snapshot_ts = _extract_snapshot_timestamp(new_df, pd_module=pd)
    previous_snapshot_display = _format_timestamp_for_display(
        previous_snapshot_ts, pd_module=pd
    )
    current_snapshot_display = _format_timestamp_for_display(
        current_snapshot_ts, pd_module=pd
    )
    elapsed_display = _format_elapsed_between_exports(
        current_snapshot_ts, previous_snapshot_ts
    )

    merged = new_df.merge(
        previous_df,
        on="entity_id",
        suffixes=("_new", "_old"),
        how="outer",
        indicator=True,
    )

    change_rows: List[Dict[str, Any]] = []

    for _, row in merged.iterrows():
        row_type = row.get("row_type_new") or row.get("row_type_old")
        url = row.get("url_new") or row.get("url_old")
        variation_url = row.get("variation_url_new") or row.get("variation_url_old")
        name = row.get("name_new") or row.get("name_old")
        variation_value = row.get("variation_value_new") or row.get(
            "variation_value_old"
        )

        if row_type == "variation":
            price_old = _to_float(row.get("variation_price_old"))
            price_new = _to_float(row.get("variation_price_new"))
            stock_old = _to_float(row.get("variation_stock_old"))
            stock_new = _to_float(row.get("variation_stock_new"))
        else:
            price_old = _to_float(row.get("price_old"))
            price_new = _to_float(row.get("price_new"))
            stock_old = _to_float(row.get("stock_old"))
            stock_new = _to_float(row.get("stock_new"))

        price_delta = None
        stock_delta = None

        if price_old is not None or price_new is not None:
            if price_old is None:
                price_delta = price_new
            elif price_new is None:
                price_delta = -price_old
            else:
                price_delta = round(price_new - price_old, 2)

        if stock_old is not None or stock_new is not None:
            if stock_old is None:
                stock_delta = stock_new
            elif stock_new is None:
                stock_delta = -stock_old
            else:
                stock_delta = round(stock_new - stock_old, 2)

        if (price_delta not in (None, 0.0)) or (stock_delta not in (None, 0.0)):
            previous_scraped = _format_timestamp_for_display(
                row.get("scraped_at_old"), pd_module=pd
            ) or previous_snapshot_display
            current_scraped = _format_timestamp_for_display(
                row.get("scraped_at_new"), pd_module=pd
            ) or current_snapshot_display

            change_rows.append(
                {
                    "entity_id": row.get("entity_id"),
                    "row_type": row_type,
                    "url": url,
                    "variation_url": variation_url,
                    "name": name,
                    "variation_value": variation_value,
                    "old_price": price_old,
                    "new_price": price_new,
                    "price_delta": price_delta,
                    "old_stock": stock_old,
                    "new_stock": stock_new,
                    "stock_delta": stock_delta,
                    "previous_scraped_at": previous_scraped,
                    "current_scraped_at": current_scraped,
                    "Прошедшее время между выгрузками (в часах и днях)": elapsed_display,
                }
            )

    if not change_rows:
        return pd.DataFrame(columns=columns)

    return pd.DataFrame(change_rows, columns=columns)


def _write_csv_exports(
    *,
    full_display: "pd.DataFrame",
    seo_dataframe: "pd.DataFrame",
    diff_dataframe: "pd.DataFrame",
    base_dir: Path,
) -> Dict[str, Path]:
    csv_paths: Dict[str, Path] = {}

    exports = {
        "full_data": full_display,
        "seo": seo_dataframe,
        "changes": diff_dataframe,
    }

    for sheet_name, frame in exports.items():
        file_name = CSV_SHEETS[sheet_name]
        target_path = base_dir / file_name
        frame.to_csv(target_path, index=False)
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
    if _PANDAS_AVAILABLE and json_path.exists():
        try:
            previous_products = json.loads(json_path.read_text(encoding="utf-8"))
            previous_dataframe = _build_full_dataframe(previous_products)
        except Exception:  # pragma: no cover - defensive guard
            previous_dataframe = None

    json_payload = json.dumps(products, ensure_ascii=False, indent=2)
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
    seo_dataframe = _build_seo_dataframe(full_dataframe)
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
            full_display.to_excel(writer, sheet_name="full_data", index=False)
            seo_dataframe.to_excel(writer, sheet_name="seo", index=False)
            diff_dataframe.to_excel(writer, sheet_name="changes", index=False)

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
