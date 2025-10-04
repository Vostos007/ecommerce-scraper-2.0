#!/usr/bin/env python3
"""TDD tests for manefa.ru fast exporter API integration."""

import pytest
import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch, MagicMock
import sys
import os

# Add project root to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

class TestManefaAPIIntegration:
    """Test suite for manefa.ru InSales API integration."""

    @pytest.fixture
    def mock_html_response(self):
        """Mock HTML response with product ID."""
        return """
        <html>
        <head>
            <meta name="insales-product-id" content="226683551">
        </head>
        <body>
            <h1>Test Product</h1>
            <div class="product-price">1250.00 руб.</div>
        </body>
        </html>
        """

    @pytest.fixture
    def mock_api_response(self):
        """Mock InSales API response with variants."""
        return {
            "status": "ok",
            "products": [{
                "id": 226683551,
                "title": "Пряжа FONTY Alpaga",
                "variants": [
                    {
                        "id": 4836535173,
                        "title": "100м/50гр",
                        "available": True,
                        "price": 1250.0,
                        "old_price": None,
                        "quantity": 1,
                        "option1": "Серый",
                        "option2": "100м/50гр"
                    },
                    {
                        "id": 4836535174,
                        "title": "100м/50гр",
                        "available": False,
                        "price": 1250.0,
                        "old_price": None,
                        "quantity": 0,
                        "option1": "Бежевый",
                        "option2": "100м/50гр"
                    }
                ]
            }]
        }

    @pytest.mark.asyncio
    async def test_fetch_insales_product_data_success(self, mock_api_response):
        """Test successful InSales API data fetching."""
        from scripts.manefa_fast_export import _fetch_insales_product_data

        # Mock the HTML response with product ID
        html_response = MagicMock()
        html_response.text = '<meta name="product-id" content="226683551">'

        # Mock the API response
        api_response = MagicMock()
        api_response.json.return_value = mock_api_response

        mock_client = AsyncMock()

        # Set up side_effect to return different responses based on URL
        async def mock_get(url, **kwargs):
            if "products_by_id" in url:
                return api_response
            else:
                return html_response

        mock_client.get.side_effect = mock_get

        url = "https://www.manefa.ru/product/pryazha-fonty-alpaga"
        result = await _fetch_insales_product_data(mock_client, url)

        assert result is not None
        assert result["id"] == 226683551
        assert len(result["variants"]) == 2
        assert result["variants"][0]["option1"] == "Серый"
        assert result["variants"][0]["price"] == 1250.0

    @pytest.mark.asyncio
    async def test_fetch_insales_product_data_no_id(self):
        """Test API fetching with invalid URL (no product ID)."""
        from scripts.manefa_fast_export import _fetch_insales_product_data

        mock_client = AsyncMock()

        url = "https://www.manefa.ru/invalid-url"
        result = await _fetch_insales_product_data(mock_client, url)

        assert result is None

    @pytest.mark.asyncio
    async def test_fetch_insales_product_data_api_error(self):
        """Test API fetching when API returns error."""
        from scripts.manefa_fast_export import _fetch_insales_product_data

        mock_client = AsyncMock()
        mock_client.get.return_value.json.return_value = {"status": "error"}

        url = "https://www.manefa.ru/product/pryazha-fonty-alpaga"
        result = await _fetch_insales_product_data(mock_client, url)

        assert result is None

    def test_extract_product_with_api_data(self, mock_html_response, mock_api_response):
        """Test product extraction with API data included."""
        from scripts.manefa_fast_export import _extract_product

        api_product_data = mock_api_response["products"][0]
        result = _extract_product(mock_html_response, "https://www.manefa.ru/product/test", api_product_data)

        # Check that variants were populated from API data
        assert len(result["variations"]) > 0  # Should have variants from API
        assert result["price"] == 1250.0  # Should use price from API variant
        assert result["in_stock"] == True  # Based on available variants

    def test_extract_product_without_api_data(self, mock_html_response):
        """Test product extraction without API data."""
        from scripts.manefa_fast_export import _extract_product

        result = _extract_product(mock_html_response, "https://www.manefa.ru/product/test", None)

        # Should still extract basic info from HTML
        assert result["url"] == "https://www.manefa.ru/product/test"
        # Variations should be empty without API data
        assert len(result["variations"]) == 0

    @pytest.mark.asyncio
    async def test_end_to_end_product_fetching(self, mock_html_response, mock_api_response):
        """Test complete product fetching with API integration."""
        from scripts.manefa_fast_export import _fetch_product

        # Mock HTML response with product ID
        html_response = MagicMock()
        html_response.text = mock_html_response

        # Mock API response
        api_response = MagicMock()
        api_response.json.return_value = mock_api_response

        mock_client = AsyncMock()

        # Mock responses
        async def mock_get(url, **kwargs):
            if "products_by_id" in url:
                return api_response
            else:
                return html_response

        mock_client.get.side_effect = mock_get

        result = await _fetch_product(mock_client, "https://www.manefa.ru/product/test")

        assert result is not None
        assert "variations" in result
        # Should have variants from API integration
        assert len(result["variations"]) > 0

if __name__ == "__main__":
    # Run tests
    pytest.main([__file__, "-v"])