"""
Comprehensive display utilities for product-variation formatting.

This module provides advanced formatting functions for displaying product variations
in various formats including hierarchical trees, tables, and summaries.
"""

import json
import os
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

from rich import box
from rich.console import Console, Group, RenderableType
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.tree import Tree

from utils.rich_themes import get_console

# Configure logger
logger = logging.getLogger(__name__)

# Internationalization dictionary
I18N = {
    "en": {
        "product": "Product",
        "variations": "Variations",
        "price": "Price",
        "stock": "Stock",
        "type": "Type",
        "value": "Value",
        "total": "Total",
        "average": "Average",
        "min": "Min",
        "max": "Max",
        "summary": "Summary",
        "no_data": "No data available",
        "error": "Error",
        "loading": "Loading...",
        "complete": "Complete",
    },
    "ru": {
        "product": "Продукт",
        "variations": "Вариации",
        "price": "Цена",
        "stock": "Запас",
        "type": "Тип",
        "value": "Значение",
        "total": "Всего",
        "average": "Среднее",
        "min": "Мин",
        "max": "Макс",
        "summary": "Сводка",
        "no_data": "Данные недоступны",
        "error": "Ошибка",
        "loading": "Загрузка...",
        "complete": "Завершено",
    },
}


@dataclass
class DisplayConfig:
    """Configuration for display formatting"""

    output_format: str = "tree"  # tree, table, compact
    max_name_length: int = 50
    currency: str = "RUB"
    language: str = "ru"
    show_colors: bool = True
    show_timestamps: bool = False
    table_style: str = "markdown"
    tree_symbols: Optional[Dict[str, str]] = None

    def __post_init__(self):
        if self.tree_symbols is None:
            self.tree_symbols = {
                "branch": "├──",
                "last": "└──",
                "vertical": "│",
                "space": "   ",
            }


class DisplayFormatter:
    """Main display formatter class"""

    def __init__(self, config_path: str = "config/settings.json"):
        self.config_path = config_path
        self.config = self._load_config()
        self.display_config = self._load_display_config()
        self.i18n = I18N.get(self.display_config.language, I18N["en"])
        self._settings_path = Path(config_path)

    def _get_console(self) -> Console:
        return get_console(settings_path=self._settings_path, record=True)

    def _render(self, *renderables: RenderableType) -> str:
        console = self._get_console()
        for renderable in renderables:
            console.print(renderable)
        return console.export_text(clear=True)

    def build_product_header(self, product_data: Dict[str, Any]) -> Text:
        name = truncate_with_ellipsis(product_data.get("name", "Unknown Product"), 50)
        base_price = product_data.get("base_price", 0.0)
        base_stock = product_data.get("stock_quantity", 0)
        variation_count = product_data.get("variation_count", 0)

        header = Text()
        header.append("📦 ", style="tree.product")
        header.append(name, style="tree.product")
        header.append(" ", style="tree.meta")
        header.append(
            f"(Base: {format_currency(base_price)}, Stock: {base_stock if base_stock is not None else 'N/A'})",
            style="tree.meta",
        )

        if variation_count and variation_count > 0:
            min_price = product_data.get("min_price")
            max_price = product_data.get("max_price")
            if min_price is not None and max_price is not None:
                header.append(
                    f" [{variation_count} variations: {format_price_range(min_price, max_price)}]",
                    style="tree.meta",
                )
        return header

    def build_variation_tree(self, variations: List[Dict[str, Any]]) -> Tree:
        root = Tree(Text("Variations", style="tree.product"))

        if not variations:
            root.add(Text("No variations available", style="muted"))
            return root

        variations_by_type: Dict[str, List[Dict[str, Any]]] = {}
        for var in variations:
            var_type = var.get("variation_type", "unknown")
            variations_by_type.setdefault(var_type, []).append(var)

        for var_type in sorted(variations_by_type.keys()):
            type_branch = root.add(Text(var_type.title(), style=_style_for_variation_type(var_type)))
            for variation in variations_by_type[var_type]:
                value = variation.get("variation_value", "-")
                price = variation.get("price")
                stock = variation.get("stock_quantity")
                detail = Text()
                detail.append(str(value), style="tree.variation")
                detail.append(" | ", style="muted")
                detail.append(format_currency(price) if price else "N/A", style="tree.meta")
                detail.append(" | Stock: ", style="muted")
                detail.append(str(stock) if stock is not None else "N/A", style="tree.meta")
                type_branch.add(detail)

        return root

    def build_variation_table(self, data: List[Dict[str, Any]]) -> Table:
        table = Table(box=box.SIMPLE_HEAVY)
        if not data:
            table.add_column("Message")
            table.add_row("No data to display")
            return table

        headers = list(data[0].keys())
        for header in headers:
            table.add_column(header.title(), style="table.header")
        for row in data:
            table.add_row(*[format_table_cell(str(row.get(col, "")), 20) for col in headers])
        return table

    def _load_config(self) -> Dict[str, Any]:
        """Load main configuration"""
        try:
            with open(self.config_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.warning(f"Failed to load config: {e}")
            return {}

    def _load_display_config(self) -> DisplayConfig:
        """Load display-specific configuration"""
        display_settings = self.config.get("display", {})

        return DisplayConfig(
            output_format=display_settings.get("output_format", "tree"),
            max_name_length=display_settings.get("max_name_length", 50),
            currency=display_settings.get("currency", "RUB"),
            language=display_settings.get("language", "ru"),
            show_colors=display_settings.get("show_colors", True),
            show_timestamps=display_settings.get("show_timestamps", False),
            table_style=display_settings.get("table_style", "markdown"),
            tree_symbols=display_settings.get("tree_symbols"),
        )


def format_hierarchical_product_display(
    products_with_variations: List[Dict[str, Any]],
) -> str:
    """
    Main formatting function for hierarchical product-variation display.

    Args:
        products_with_variations: List of product dictionaries with variations

    Returns:
        Formatted string representation
    """
    if not products_with_variations:
        return "No products to display"

    formatter = DisplayFormatter()
    renderables: List[RenderableType] = []

    for product in products_with_variations:
        try:
            header_text = formatter.build_product_header(product)
            renderables.append(header_text)
            tree = formatter.build_variation_tree(product.get("variations", []))
            renderables.append(tree)
            renderables.append(Text(""))
        except Exception as e:
            logger.error(
                f"Error formatting product {product.get('id', 'unknown')}: {e}"
            )
            renderables.append(Text(f"Error formatting product: {str(e)}", style="error"))

    return formatter._render(*renderables)


def create_product_header(product_data: Dict[str, Any]) -> str:
    """
    Format product information header.

    Args:
        product_data: Product data dictionary

    Returns:
        Formatted header string
    """
    try:
        formatter = DisplayFormatter()
        header = formatter.build_product_header(product_data)
        return formatter._render(header).strip()

    except Exception as e:
        logger.error(f"Error creating product header: {e}")
        return f"📦 Error formatting product header: {str(e)}"


def create_variation_tree(variations: List[Dict[str, Any]]) -> str:
    """
    Create tree-like variation display.

    Args:
        variations: List of variation dictionaries

    Returns:
        Formatted tree string
    """
    try:
        formatter = DisplayFormatter()
        tree = formatter.build_variation_tree(variations)
        return formatter._render(tree).rstrip()

    except Exception as e:
        logger.error(f"Error creating variation tree: {e}")
        return f"   └── Error formatting variations: {str(e)}"


def format_price_range(min_price: float, max_price: float) -> str:
    """
    Format price ranges.

    Args:
        min_price: Minimum price
        max_price: Maximum price

    Returns:
        Formatted price range string
    """
    try:
        if min_price == max_price:
            return format_currency(min_price)
        else:
            return f"{format_currency(min_price)} - {format_currency(max_price)}"
    except Exception as e:
        logger.error(f"Error formatting price range: {e}")
        return "Price range error"


def format_stock_summary(base_stock: int, variation_stocks: List[int]) -> str:
    """
    Summarize stock information.

    Args:
        base_stock: Base product stock
        variation_stocks: List of variation stocks

    Returns:
        Formatted stock summary
    """
    try:
        total_stock = base_stock or 0
        if variation_stocks:
            variation_total = sum(s for s in variation_stocks if s is not None)
            total_stock += variation_total

        if not variation_stocks:
            return f"Total Stock: {total_stock}"

        variation_count = len([s for s in variation_stocks if s is not None])
        avg_stock = (
            total_stock / (variation_count + 1) if variation_count > 0 else total_stock
        )

        return f"Total: {total_stock}, Avg: {avg_stock:.1f}"

    except Exception as e:
        logger.error(f"Error formatting stock summary: {e}")
        return "Stock summary error"


def get_tree_symbols() -> Dict[str, str]:
    """
    Return Unicode tree drawing characters.

    Returns:
        Dictionary of tree symbols
    """
    return {"branch": "├──", "last": "└──", "vertical": "│", "space": "   "}


def _style_for_variation_type(var_type: str) -> str:
    palette = {
        "size": "table.success",
        "color": "table.header",
        "model": "accent",
        "material": "magenta",
        "style": "cyan",
    }
    return palette.get(var_type.lower(), "table.neutral")


def colorize_variation_type(var_type: str) -> str:
    """
    Add color coding for different types.

    Args:
        var_type: Variation type string

    Returns:
        Colorized string
    """
    try:
        style = _style_for_variation_type(var_type)
        return f"[{style}]{var_type.title()}[/]"

    except Exception as e:
        logger.error(f"Error colorizing variation type: {e}")
        return var_type


def format_currency(amount: Union[float, int], currency: str = "RUB") -> str:
    """
    Consistent currency formatting.

    Args:
        amount: Amount to format
        currency: Currency code

    Returns:
        Formatted currency string
    """
    try:
        if amount is None:
            return "N/A"

        # Currency symbols
        symbols = {"RUB": "₽", "USD": "$", "EUR": "€", "GBP": "£"}

        symbol = symbols.get(currency, currency)
        return f"{amount:,.2f} {symbol}"

    except Exception as e:
        logger.error(f"Error formatting currency: {e}")
        return f"{amount} {currency}"


def truncate_with_ellipsis(text: str, max_length: int) -> str:
    """
    Handle long product names.

    Args:
        text: Text to truncate
        max_length: Maximum length

    Returns:
        Truncated text with ellipsis
    """
    try:
        if not text or len(text) <= max_length:
            return text or ""

        return text[: max_length - 3] + "..."

    except Exception as e:
        logger.error(f"Error truncating text: {e}")
        return str(text)[:max_length] if text else ""


def create_markdown_table_grouped(data: List[Dict[str, Any]]) -> str:
    """
    Create grouped markdown tables.

    Args:
        data: List of data dictionaries

    Returns:
        Markdown table string
    """
    if not data:
        return "No data to display"

    try:
        formatter = DisplayFormatter()
        renderables: List[RenderableType] = []

        if not data:
            table = Table(box=box.SIMPLE)
            table.add_column("Message")
            table.add_row("No data to display")
            return formatter._render(table).strip()

        grouped_data: Dict[str, List[Dict[str, Any]]] = {}
        for item in data:
            product_id = str(item.get("product_id", "unknown"))
            grouped_data.setdefault(product_id, []).append(item)

        for product_id, items in grouped_data.items():
            product_name = items[0].get("product_name", f"Product {product_id}")
            renderables.append(Text(f"### {product_name}", style="table.header"))

            table = Table(box=box.SIMPLE_HEAD, highlight=True)
            headers = list(items[0].keys())
            for header in headers:
                table.add_column(header.replace("_", " ").title(), style="table.header")
            for row in items:
                table.add_row(*[str(row.get(header, "")) for header in headers])
            renderables.append(table)
            renderables.append(Text("---", style="muted"))

        return formatter._render(*renderables)

    except Exception as e:
        logger.error(f"Error creating markdown table: {e}")
        return f"Error creating table: {str(e)}"


def calculate_column_widths(data: List[List[Any]]) -> List[int]:
    """
    Optimize table column sizing.

    Args:
        data: Table data as list of lists

    Returns:
        List of column widths
    """
    try:
        if not data:
            return []

        num_cols = len(data[0]) if data else 0
        widths = [0] * num_cols

        for row in data:
            for i, cell in enumerate(row):
                if i < num_cols:
                    cell_str = str(cell)
                    widths[i] = max(widths[i], len(cell_str))

        # Minimum width of 10, maximum of 50
        return [max(10, min(50, w)) for w in widths]

    except Exception as e:
        logger.error(f"Error calculating column widths: {e}")
        return [20] * (len(data[0]) if data else 5)


def add_table_separators(rows: List[List[str]]) -> List[List[str]]:
    """
    Add visual separators between products.

    Args:
        rows: Table rows

    Returns:
        Rows with separators
    """
    try:
        if not rows:
            return rows

        result = []
        current_product = None

        for row in rows:
            if row and len(row) > 0:
                product_id = row[0]  # Assume first column is product identifier

                if current_product and current_product != product_id:
                    # Add separator row
                    separator = ["---"] * len(row)
                    result.append(separator)

                current_product = product_id

            result.append(row)

        return result

    except Exception as e:
        logger.error(f"Error adding table separators: {e}")
        return rows


def format_table_cell(content: str, width: int, alignment: str = "left") -> str:
    """
    Consistent cell formatting.

    Args:
        content: Cell content
        width: Cell width
        alignment: left, center, right

    Returns:
        Formatted cell content
    """
    try:
        content_str = str(content)

        if len(content_str) > width:
            content_str = content_str[: width - 3] + "..."

        if alignment == "center":
            return content_str.center(width)
        elif alignment == "right":
            return content_str.rjust(width)
        else:  # left
            return content_str.ljust(width)

    except Exception as e:
        logger.error(f"Error formatting table cell: {e}")
        return str(content)[:width].ljust(width)


def calculate_product_statistics(
    product_with_variations: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Calculate per-product statistics.

    Args:
        product_with_variations: Product data with variations

    Returns:
        Statistics dictionary
    """
    try:
        variations = product_with_variations.get("variations", [])
        base_price = product_with_variations.get("base_price", 0.0)
        base_stock = product_with_variations.get("stock_quantity", 0)

        stats = {
            "variation_count": len(variations),
            "base_price": base_price,
            "base_stock": base_stock,
            "has_variations": len(variations) > 0,
        }

        if variations:
            prices = [
                v.get("price", 0.0) for v in variations if v.get("price") is not None
            ]
            stocks = [
                v.get("stock_quantity", 0)
                for v in variations
                if v.get("stock_quantity") is not None
            ]

            stats.update(
                {
                    "min_variation_price": min(prices) if prices else None,
                    "max_variation_price": max(prices) if prices else None,
                    "avg_variation_price": (
                        sum(prices) / len(prices) if prices else None
                    ),
                    "total_variation_stock": sum(stocks) if stocks else 0,
                    "avg_variation_stock": sum(stocks) / len(stocks) if stocks else 0,
                }
            )

        return stats

    except Exception as e:
        logger.error(f"Error calculating product statistics: {e}")
        return {"error": str(e)}


def generate_variation_summary(variations: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Variation type distribution.

    Args:
        variations: List of variations

    Returns:
        Summary dictionary
    """
    try:
        if not variations:
            return {"total_variations": 0, "types": {}}

        type_counts = {}
        type_prices = {}
        type_stocks = {}

        for var in variations:
            var_type = var.get("variation_type", "unknown")
            price = var.get("price")
            stock = var.get("stock_quantity")

            type_counts[var_type] = type_counts.get(var_type, 0) + 1

            if price is not None:
                if var_type not in type_prices:
                    type_prices[var_type] = []
                type_prices[var_type].append(price)

            if stock is not None:
                if var_type not in type_stocks:
                    type_stocks[var_type] = []
                type_stocks[var_type].append(stock)

        summary = {
            "total_variations": len(variations),
            "unique_types": len(type_counts),
            "types": {},
        }

        for var_type, count in type_counts.items():
            type_summary = {
                "count": count,
                "percentage": (count / len(variations)) * 100,
            }

            if var_type in type_prices:
                prices = type_prices[var_type]
                type_summary.update(
                    {
                        "avg_price": sum(prices) / len(prices),
                        "min_price": min(prices),
                        "max_price": max(prices),
                    }
                )

            if var_type in type_stocks:
                stocks = type_stocks[var_type]
                type_summary.update(
                    {"total_stock": sum(stocks), "avg_stock": sum(stocks) / len(stocks)}
                )

            summary["types"][var_type] = type_summary

        return summary

    except Exception as e:
        logger.error(f"Error generating variation summary: {e}")
        return {"error": str(e)}


def format_performance_metrics(metrics: Dict[str, Any]) -> str:
    """
    Display performance data.

    Args:
        metrics: Performance metrics dictionary

    Returns:
        Formatted metrics string
    """
    try:
        output = ["## Performance Metrics", ""]

        for key, value in metrics.items():
            if isinstance(value, float):
                output.append(f"- {key}: {value:.2f}")
            elif isinstance(value, int):
                output.append(f"- {key}: {value:,}")
            else:
                output.append(f"- {key}: {value}")

        return "\n".join(output)

    except Exception as e:
        logger.error(f"Error formatting performance metrics: {e}")
        return f"Error formatting metrics: {str(e)}"


def create_summary_footer(total_stats: Dict[str, Any]) -> str:
    """
    Overall summary information.

    Args:
        total_stats: Total statistics

    Returns:
        Formatted footer string
    """
    try:
        output = ["", "---", "## Summary", ""]

        for key, value in total_stats.items():
            formatted_key = key.replace("_", " ").title()
            if isinstance(value, float):
                output.append(f"- {formatted_key}: {value:.2f}")
            elif isinstance(value, int):
                output.append(f"- {formatted_key}: {value:,}")
            else:
                output.append(f"- {formatted_key}: {value}")

        output.append("")
        output.append(f"Generated at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

        return "\n".join(output)

    except Exception as e:
        logger.error(f"Error creating summary footer: {e}")
        return f"Error creating footer: {str(e)}"


# Utility functions for validation and error handling


def validate_display_data(data: Any, data_type: str) -> bool:
    """
    Validate display data integrity.

    Args:
        data: Data to validate
        data_type: Type of data ('product', 'variation', etc.)

    Returns:
        True if valid, False otherwise
    """
    try:
        if data_type == "product":
            required_fields = ["id", "name"]
            if not isinstance(data, dict):
                return False
            return all(field in data for field in required_fields)

        elif data_type == "variation":
            required_fields = ["variation_type", "variation_value"]
            if not isinstance(data, dict):
                return False
            return all(field in data for field in required_fields)

        return True

    except Exception as e:
        logger.error(f"Error validating {data_type} data: {e}")
        return False


def handle_display_error(error: Exception, context: str) -> str:
    """
    Handle display errors gracefully.

    Args:
        error: Exception that occurred
        context: Context where error occurred

    Returns:
        User-friendly error message
    """
    logger.error(f"Display error in {context}: {error}")
    return f"Error in {context}: {str(error)}"


def get_fallback_display(data_type: str) -> str:
    """
    Provide fallback formatting for edge cases.

    Args:
        data_type: Type of data

    Returns:
        Fallback display string
    """
    fallbacks = {
        "product": "📦 [Product data unavailable]",
        "variation": "   └── [Variation data unavailable]",
        "table": "| Error | Loading data... |",
        "summary": "Summary data unavailable",
    }

    return fallbacks.get(data_type, "[Data unavailable]")


def debug_display_info(data: Any, label: str) -> None:
    """
    Include debug information for troubleshooting.

    Args:
        data: Data to debug
        label: Debug label
    """
    if logger.isEnabledFor(logging.DEBUG):
        logger.debug(
            f"Debug {label}: type={type(data)}, len={len(data) if hasattr(data, '__len__') else 'N/A'}"
        )
        if isinstance(data, dict):
            logger.debug(f"Debug {label} keys: {list(data.keys())}")
