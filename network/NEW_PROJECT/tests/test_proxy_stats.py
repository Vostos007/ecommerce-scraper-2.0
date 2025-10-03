from __future__ import annotations

import json
from pathlib import Path

from ..proxy_stats.collector import collect_proxy_stats


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_collect_proxy_stats(tmp_path: Path) -> None:
    repo = tmp_path

    # Prepare proxy configurations
    _write(
        repo / "config" / "manual_proxies.txt",
        "\n".join(
            [
                "https://user:pass@80.0.0.1:8000",
                "socks5://user:pass@45.1.1.1:9000",
            ]
        ),
    )
    _write(repo / "config" / "proxies_https.txt", "80.0.0.1:8000:user:pass\n147.1.1.1:8080:alt:altpass")
    _write(
        repo / "config" / "proxy" / "proxy_pools.yml",
        json.dumps(
            {
                "datacenter": {"list": ["https://extra:pwd@91.2.2.2:7000"]},
                "residential": {"list": []},
            }
        ),
    )

    # Firecrawl summary with two sites
    summary = {
        "example.ru": {
            "products": 120,
            "products_with_price": 110,
            "success_rate": 95.0,
            "updated_at": "2025-10-01T10:00:00Z",
        },
        "shop.ru": {
            "products": 80,
            "products_with_price": 72,
            "success_rate": 90.0,
            "updated_at": "2025-10-02T12:30:00Z",
        },
    }
    _write(repo / "reports" / "firecrawl_baseline_summary.json", json.dumps(summary))

    # Logs with failure and burned proxy
    _write(
        repo / "logs" / "antibot.log",
        "\n".join(
            [
                "2025-10-02 12:00:00,000 - info - Proxy failure handled: https://user:pass@80.0.0.1:8000",
                "2025-10-02 12:05:00,000 - warning - Marked proxy as burned https://extra:pwd@91.2.2.2:7000",
            ]
        ),
    )

    stats = collect_proxy_stats(repo)

    assert stats["total_proxies"] == 4
    assert stats["failed_proxies"] == 1
    assert stats["burned_proxies"] == 1
    assert stats["healthy_proxies"] == 3
    assert stats["total_requests"] == 200
    assert stats["successful_requests"] == 182
    assert 0 < stats["success_rate"] <= 100

    autoscale = stats["autoscale"]
    assert isinstance(autoscale, dict)
    assert autoscale["optimal_proxy_count"] >= stats["healthy_proxies"]

    premium = stats["premium_proxy_stats"]
    assert premium is not None
    assert premium["bandwidth"] > 0
    assert premium["monthly_budget"] >= premium["cost"]

    top = stats["top_performing_proxies"]
    assert len(top) > 0
    assert "://" in top[0]["proxy"]

    generated_at = stats["generated_at"]
    assert isinstance(generated_at, str)
    assert generated_at.endswith("Z") or "+" in generated_at
