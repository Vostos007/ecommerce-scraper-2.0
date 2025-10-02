"""
Базовые типы данных для проекта CompetitorMonitor RU.

Этот модуль содержит все базовые типы, Protocol классы и аннотации,
используемые в проекте для строгой типизации.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import (
    Any,
    Dict,
    List,
    Literal,
    Optional,
    Protocol,
    TypedDict,
    Union,
    TypeVar,
    Generic,
    Callable,
    runtime_checkable,
)

try:  # Python 3.11+
    from typing import NotRequired
except ImportError:  # Python 3.10 compatibility
    from typing_extensions import NotRequired

from pydantic import BaseModel, ConfigDict as PydanticConfigDict, Field, field_validator
import aiohttp


# ============================================================================
# Базовые типы данных
# ============================================================================

# Типы для URL и идентификаторов
URL = str
ProductID = int
SiteDomain = str
VariationID = int
UserAgent = str
ProxyURL = str

# Коды ответов HTTP
HTTPStatusCode = int
ResponseContent = str
HTMLContent = str
JSONContent = Dict[str, Any]

# Конфигурационные типы
ConfigDict = Dict[str, Any]
Headers = Dict[str, str]
Cookies = Dict[str, str]
RequestParams = Dict[str, Any]

# Типы для метрик и мониторинга
Timestamp = float
PerformanceMetrics = Dict[str, Union[float, int, str]]
StockQuantity = int
Price = float


# ============================================================================
# Progress reporting primitives
# ============================================================================


PHASE_DISCOVERY = "discovery"
PHASE_SCRAPING = "scraping"
PHASE_COMPLETE = "complete"


@dataclass
class ProgressEvent:
    """Represents a progress update emitted during scraping."""

    phase: str
    current: int
    total: int
    message: Optional[str] = None


ProgressCallback = Callable[["ProgressEvent"], None]


# ============================================================================
# Enums для состояний и типов
# ============================================================================


class ScrapingMethod(str, Enum):
    """Методы скрапинга."""

    HTTPX = "httpx"
    PLAYWRIGHT = "playwright"
    HYBRID = "hybrid"
    AUTO = "auto"


class VariationType(str, Enum):
    """Типы вариаций товаров."""

    SIZE = "size"
    COLOR = "color"
    MATERIAL = "material"
    STYLE = "style"
    WEIGHT = "weight"
    VOLUME = "volume"
    PATTERN = "pattern"
    BRAND = "brand"
    MODEL = "model"
    CUSTOM = "custom"


class SiteType(str, Enum):
    """Типы CMS сайтов."""

    WOOCOMMERCE = "woocommerce"
    SHOPIFY = "shopify"
    OPENCART = "opencart"
    MAGENTO = "magento"
    PRESTASHOP = "prestashop"
    CUSTOM = "custom"
    UNKNOWN = "unknown"


class StockStatus(str, Enum):
    """Статусы наличия товара."""

    IN_STOCK = "in_stock"
    OUT_OF_STOCK = "out_of_stock"
    LIMITED_STOCK = "limited_stock"
    PREORDER = "preorder"
    UNKNOWN = "unknown"


class ProcessingMode(str, Enum):
    """Режимы обработки данных."""

    BATCH = "batch"
    SEQUENTIAL = "sequential"
    HYBRID_PROCESSING = "hybrid_processing"


# ============================================================================
# TypedDict модели для данных
# ============================================================================


class ProductData(TypedDict):
    """Базовые данные товара."""

    url: URL
    name: str
    base_price: Price
    in_stock: bool
    stock_quantity: StockQuantity
    site_domain: SiteDomain
    scraped_at: str
    price: NotRequired[Price]
    stock: NotRequired[StockQuantity]
    variations: NotRequired[List["VariationData"]]
    error: NotRequired[Optional[str]]
    seo_h1: NotRequired[Optional[str]]
    seo_title: NotRequired[Optional[str]]
    seo_meta_description: NotRequired[Optional[str]]


class VariationData(TypedDict):
    """Данные вариации товара."""

    variation_type: VariationType
    variation_value: str
    price: Price
    stock_quantity: StockQuantity
    sku: Optional[str]


class ScrapeResult(TypedDict):
    """Результат скрапинга."""

    success: bool
    method_used: ScrapingMethod
    response_time: float
    status_code: Optional[HTTPStatusCode]
    error_message: Optional[str]
    products_found: int
    variations_found: int
    timestamp: Timestamp
    total_urls_found: NotRequired[int]
    scraped_products: NotRequired[int]
    variations: NotRequired[int]
    failures: NotRequired[Dict[str, str]]
    products: NotRequired[List[ProductData]]
    export_path: NotRequired[str]
    export_path_excel: NotRequired[str]
    avg_response_time: NotRequired[float]
    success_rate: NotRequired[float]


class ProxyConfig(TypedDict):
    """Конфигурация прокси."""

    enabled: bool
    rotation_enabled: bool
    proxy_list: List[ProxyURL]
    timeout: int
    max_retries: int


class AntibotConfig(TypedDict):
    """Конфигурация анти-бот системы."""

    enabled: bool
    user_agent_rotation: bool
    delay_between_requests: float
    max_concurrent_requests: int
    stealth_mode: bool


# ============================================================================
# Pydantic модели для валидации
# ============================================================================


class BaseProductModel(BaseModel):
    """Базовая модель товара с валидацией."""

    url: URL = Field(..., description="URL товара")
    name: str = Field(..., min_length=1, max_length=500, description="Название товара")
    base_price: Price = Field(..., ge=0, description="Базовая цена товара")
    in_stock: bool = Field(..., description="Наличие товара")
    stock_quantity: StockQuantity = Field(..., ge=0, description="Количество на складе")
    site_domain: SiteDomain = Field(..., description="Домен сайта")

    @field_validator("url")
    @classmethod
    def validate_url(cls, v: str) -> str:
        """Валидация URL."""
        if not v.startswith(("http://", "https://")):
            raise ValueError("URL должен начинаться с http:// или https://")
        return v

    @field_validator("site_domain")
    @classmethod
    def validate_domain(cls, v: str) -> str:
        """Валидация домена."""
        if not v or "." not in v:
            raise ValueError("Некорректный домен сайта")
        return v.lower()


class ProductVariationModel(BaseModel):
    """Модель вариации товара с валидацией."""

    product_id: ProductID = Field(..., description="ID товара")
    variation_type: VariationType = Field(..., description="Тип вариации")
    variation_value: str = Field(
        ..., min_length=1, max_length=200, description="Значение вариации"
    )
    price: Price = Field(..., ge=0, description="Цена вариации")
    stock_quantity: StockQuantity = Field(..., ge=0, description="Количество на складе")
    sku: Optional[str] = Field(None, max_length=100, description="SKU товара")

    model_config = PydanticConfigDict(use_enum_values=True)


class ScrapeConfigModel(BaseModel):
    """Модель конфигурации скрапинга с валидацией."""

    method: ScrapingMethod = Field(ScrapingMethod.AUTO, description="Метод скрапинга")
    max_products: int = Field(
        50, ge=1, le=1000, description="Максимальное количество товаров"
    )
    max_concurrent: int = Field(
        5, ge=1, le=20, description="Максимальное количество одновременных запросов"
    )
    delay_between_requests: float = Field(
        1.0, ge=0.1, le=10.0, description="Задержка между запросами"
    )
    timeout: int = Field(30, ge=5, le=120, description="Таймаут запроса")
    retry_attempts: int = Field(3, ge=1, le=10, description="Количество попыток")

    model_config = PydanticConfigDict(use_enum_values=True)


# ============================================================================
# Protocol классы для интерфейсов
# ============================================================================


@runtime_checkable
class ScraperProtocol(Protocol):
    """Протокол для скраперов."""

    async def scrape_product(self, url: URL) -> Optional[ProductData]:
        """Скрапинг одного товара."""
        ...

    async def scrape_multiple(self, urls: List[URL]) -> List[ProductData]:
        """Скрапинг нескольких товаров."""
        ...

    async def close(self) -> None:
        """Закрытие ресурсов скрапера."""
        ...


@runtime_checkable
class DatabaseProtocol(Protocol):
    """Протокол для работы с базой данных."""

    async def insert_product(self, product: ProductData) -> ProductID:
        """Вставка товара в БД."""
        ...

    async def get_product(self, product_id: ProductID) -> Optional[ProductData]:
        """Получение товара из БД."""
        ...

    async def update_product(self, product_id: ProductID, data: ProductData) -> bool:
        """Обновление товара в БД."""
        ...


@runtime_checkable
class ParserProtocol(Protocol):
    """Протокол для парсеров товаров."""

    def parse_product(self, html: HTMLContent, url: URL) -> Optional[ProductData]:
        """Парсинг данных товара из HTML."""
        ...

    def parse_variations(self, html: HTMLContent) -> List[VariationData]:
        """Парсинг вариаций товара из HTML."""
        ...


@runtime_checkable
class AntibotProtocol(Protocol):
    """Протокол для анти-бот системы."""

    async def get_headers(self) -> Headers:
        """Получение заголовков для запроса."""
        ...

    async def get_proxy(self) -> Optional[ProxyURL]:
        """Получение прокси для запроса."""
        ...

    async def handle_block(self, url: URL, response_code: HTTPStatusCode) -> bool:
        """Обработка блокировки."""
        ...


@runtime_checkable
class MonitorProtocol(Protocol):
    """Протокол для мониторинга."""

    async def check_stock_changes(self, product_id: ProductID) -> bool:
        """Проверка изменений в наличии товара."""
        ...

    async def send_notification(self, message: str, product_data: ProductData) -> bool:
        """Отправка уведомления."""
        ...


# ============================================================================
# Generic типы для асинхронной обработки
# ============================================================================

T = TypeVar("T")
ResponseType = TypeVar("ResponseType")


class AsyncResult(Generic[T]):
    """Обертка для асинхронных результатов."""

    def __init__(self, result: Optional[T] = None, error: Optional[Exception] = None):
        self.result = result
        self.error = error
        self.success = error is None

    def unwrap(self) -> T:
        """Получение результата или выбрасывание исключения."""
        if self.error:
            raise self.error
        if self.result is None:
            raise ValueError("Результат не установлен")
        return self.result


# ============================================================================
# Специализированные типы для конкретных компонентов
# ============================================================================

# Типы для HTTP клиентов
HTTPSession = Union[aiohttp.ClientSession, Any]
RequestMethod = Literal["GET", "POST", "PUT", "DELETE", "PATCH"]

# Типы для Playwright
PlaywrightPage = Any  # from playwright.async_api import Page
PlaywrightBrowser = Any  # from playwright.async_api import Browser


# Типы для конфигурации
class DatabaseConfig(TypedDict):
    """Конфигурация базы данных."""

    connection_string: str
    pool_size: int
    max_overflow: int
    pool_timeout: int


class WebhookConfig(TypedDict):
    """Конфигурация вебхуков."""

    enabled: bool
    urls: List[URL]
    secret_key: Optional[str]
    timeout: int


# Типы для результатов анализа
class PerformanceAnalysis(TypedDict):
    """Результат анализа производительности."""

    method: ScrapingMethod
    avg_response_time: float
    success_rate: float
    error_count: int
    recommendations: List[str]


class StockAnalysis(TypedDict):
    """Результат анализа складских остатков."""

    product_id: ProductID
    current_stock: StockQuantity
    stock_trend: Literal["increasing", "decreasing", "stable"]
    predicted_stock: StockQuantity
    confidence: float


# ============================================================================
# Экспорт всех типов
# ============================================================================

__all__ = [
    # Базовые типы
    "URL",
    "ProductID",
    "SiteDomain",
    "VariationID",
    "UserAgent",
    "ProxyURL",
    "HTTPStatusCode",
    "ResponseContent",
    "HTMLContent",
    "JSONContent",
    "ConfigDict",
    "Headers",
    "Cookies",
    "RequestParams",
    "Timestamp",
    "PerformanceMetrics",
    "StockQuantity",
    "Price",
    # Enums
    "ScrapingMethod",
    "VariationType",
    "SiteType",
    "StockStatus",
    "ProcessingMode",
    # TypedDict модели
    "ProductData",
    "VariationData",
    "ScrapeResult",
    "ProxyConfig",
    "AntibotConfig",
    "DatabaseConfig",
    "WebhookConfig",
    "PerformanceAnalysis",
    "StockAnalysis",
    # Pydantic модели
    "BaseProductModel",
    "ProductVariationModel",
    "ScrapeConfigModel",
    # Protocols
    "ScraperProtocol",
    "DatabaseProtocol",
    "ParserProtocol",
    "AntibotProtocol",
    "MonitorProtocol",
    # Generic типы
    "AsyncResult",
    "T",
    "ResponseType",
    # Специализированные типы
    "HTTPSession",
    "RequestMethod",
    "PlaywrightPage",
    "PlaywrightBrowser",
]
