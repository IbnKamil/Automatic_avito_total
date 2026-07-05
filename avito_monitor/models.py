from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class Listing:
    avito_id: int
    title: str
    price: int | None
    price_string: str
    url: str
    location: str
    city: str
    image_url: str
    posted_at: str | None = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False)


@dataclass
class ScanResult:
    scan_id: int
    scanned_at: datetime
    product: str
    region: str
    listings: list[Listing]
    total_found: int
    pages_scanned: int
    source: str


@dataclass
class MarketStats:
    total_listings: int
    listings_with_price: int
    min_price: int | None
    max_price: int | None
    mean_price: float | None
    median_price: float | None
    std_price: float | None
    avg_price_per_city: dict[str, float]
    listings_by_city: dict[str, int]
    price_percentiles: dict[str, float]
    top_cheapest: list[dict[str, Any]]
    top_expensive: list[dict[str, Any]]
    historical_trend: list[dict[str, Any]]
