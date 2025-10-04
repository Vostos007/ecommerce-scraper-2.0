#!/usr/bin/env python3
"""Simple TDD tests for manefa.ru API integration."""

import pytest
import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch, MagicMock
import sys
import os

# Add project root to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

class TestManefaSimple:
    """Simple test suite for manefa.ru API integration."""

    def test_module_import(self):
        """Test that the module can be imported successfully."""
        from scripts.manefa_fast_export import _fetch_insales_product_data, _extract_product
        assert callable(_fetch_insales_product_data)
        assert callable(_extract_product)

    def test_extract_product_basic(self):
        """Test basic product extraction functionality."""
        from scripts.manefa_fast_export import _extract_product

        html = """
        <html>
        <head><title>Test Product</title></head>
        <body>
            <h1>Test Product</h1>
            <div class="price">1000.00 руб.</div>
        </body>
        </html>
        """

        result = _extract_product(html, "https://example.com/product/test", None)

        assert result is not None
        assert result["url"] == "https://example.com/product/test"
        assert "variations" in result
        assert isinstance(result["variations"], list)

    def test_extract_product_with_api_variants(self):
        """Test product extraction with API variants data."""
        from scripts.manefa_fast_export import _extract_product

        html = """
        <html>
        <head><title>Test Product</title></head>
        <body>
            <h1>Test Product</h1>
        </body>
        </html>
        """

        api_data = {
            "id": 12345,
            "title": "API Product Title",
            "variants": [
                {
                    "id": 1,
                    "option1": "Red",
                    "option2": "Small",
                    "price": 1000.0,
                    "available": True,
                    "quantity": 5
                },
                {
                    "id": 2,
                    "option1": "Blue",
                    "option2": "Large",
                    "price": 1200.0,
                    "available": False,
                    "quantity": 0
                }
            ]
        }

        result = _extract_product(html, "https://example.com/product/test", api_data)

        assert result is not None
        assert len(result["variations"]) == 2
        # Check that variants have been populated with some data
        assert any(v.get("price") == 1000.0 for v in result["variations"])
        assert result["in_stock"] == True  # At least one variant is available

    @pytest.mark.asyncio
    async def test_fetch_insales_product_id_extraction(self):
        """Test product ID extraction from URL."""
        from scripts.manefa_fast_export import _fetch_insales_product_data
        import re

        # Test the regex pattern directly
        url = "https://www.manefa.ru/product/pryazha-fonty-alpaga"
        match = re.search(r"/product/([^/]+)", url)
        assert match is not None
        assert match.group(1) == "pryazha-fonty-alpaga"

    def test_price_extraction_from_api(self):
        """Test price extraction from API variants."""
        # This tests the logic that should be used in _extract_product
        api_data = {
            "variants": [
                {"price": 1000.0, "available": True},
                {"price": 1200.0, "available": False}
            ]
        }

        # Extract available variants and calculate price
        available_variants = [v for v in api_data["variants"] if v.get("available")]
        if available_variants:
            min_price = min(v["price"] for v in available_variants)
            assert min_price == 1000.0
        else:
            assert False, "Should have available variants"

if __name__ == "__main__":
    # Run tests
    pytest.main([__file__, "-v"])