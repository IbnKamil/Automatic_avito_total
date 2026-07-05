from __future__ import annotations

import json
import logging
import random
import re
import time
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import quote, urljoin

import requests
from bs4 import BeautifulSoup

from avito_monitor.config import AppConfig
from avito_monitor.models import Listing, ScanResult

logger = logging.getLogger(__name__)

AVITO_BASE = "https://www.avito.ru"
LOCATIONS_API = (
    "https://www.avito.ru/api/2/locations/top/children"
    "?includeRefs=1&key=7myrcPqWwSzInUCSvdwrtWcibz8pwaSNEMlRturi"
)
ITEMS_API = "https://www.avito.ru/web/1/main/items"
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "AVITO 52.0 (iPad5,1; 11.3.1; ru_RU)",
]


class AvitoScraperError(Exception):
    pass


class AvitoBlockedError(AvitoScraperError):
    pass


def resolve_location_id(region_name: str) -> tuple[str, int]:
    response = requests.get(
        LOCATIONS_API,
        headers={"User-Agent": random.choice(USER_AGENTS)},
        timeout=30,
    )
    response.raise_for_status()
    needle = region_name.strip().lower()
    for entry in response.json():
        names = entry.get("names", {})
        matched = any(needle in str(name).lower() for name in names.values())
        if matched:
            location_id = int(entry["id"])
            detail = requests.get(
                f"https://www.avito.ru/api/2/locations/{location_id}",
                params={"key": "7myrcPqWwSzInUCSvdwrtWcibz8pwaSNEMlRturi"},
                headers={"User-Agent": random.choice(USER_AGENTS)},
                timeout=30,
            )
            detail.raise_for_status()
            slug = detail.json().get("slug") or region_name.lower()
            return slug, location_id
    raise AvitoScraperError(f"Регион не найден на Авито: {region_name}")


def _extract_city(location: str) -> str:
    if not location:
        return "Не указан"
    parts = [part.strip() for part in location.split(",") if part.strip()]
    if len(parts) >= 2:
        return parts[1]
    return parts[0]


def _parse_price(value: Any) -> tuple[int | None, str]:
    if value is None:
        return None, "Цена не указана"
    if isinstance(value, dict):
        amount = value.get("value")
        label = value.get("string") or value.get("fullString") or ""
        if amount is None and label:
            digits = re.sub(r"[^\d]", "", label)
            amount = int(digits) if digits else None
        return (int(amount) if amount is not None else None), label or "Цена не указана"
    if isinstance(value, (int, float)):
        amount = int(value)
        return amount, f"{amount:,}".replace(",", " ") + " ₽"
    if isinstance(value, str):
        digits = re.sub(r"[^\d]", "", value)
        amount = int(digits) if digits else None
        return amount, value
    return None, "Цена не указана"


def _item_from_api_payload(item: dict[str, Any]) -> Listing | None:
    avito_id = item.get("id")
    if not avito_id:
        return None
    title = item.get("title") or item.get("name") or "Без названия"
    price, price_string = _parse_price(
        item.get("priceDetailed") or item.get("price") or item.get("priceValue")
    )
    geo = item.get("geo") or {}
    location = (
        geo.get("formattedAddress")
        or item.get("location")
        or item.get("address")
        or ""
    )
    url_path = item.get("urlPath") or item.get("url") or ""
    if url_path and not url_path.startswith("http"):
        url = urljoin(AVITO_BASE, url_path)
    else:
        url = url_path or f"{AVITO_BASE}/item/{avito_id}"
    images = item.get("images") or []
    image_url = ""
    if images:
        first = images[0]
        if isinstance(first, dict):
            image_url = next(iter(first.values()), "")
        elif isinstance(first, str):
            image_url = first
    posted_at = None
    sort_ts = item.get("sortTimeStamp") or item.get("time")
    if sort_ts:
        try:
            posted_at = datetime.fromtimestamp(int(sort_ts)).isoformat()
        except (TypeError, ValueError, OSError):
            posted_at = str(sort_ts)
    return Listing(
        avito_id=int(avito_id),
        title=title.strip(),
        price=price,
        price_string=price_string,
        url=url,
        location=location,
        city=_extract_city(location),
        image_url=image_url,
        posted_at=posted_at,
        raw=item,
    )


def _parse_html_listings(html: str) -> list[Listing]:
    soup = BeautifulSoup(html, "lxml")
    listings: list[Listing] = []
    cards = soup.select('[data-marker="item"]')
    for card in cards:
        link = card.select_one('[data-marker="item-title"]')
        if not link:
            link = card.select_one("a[itemprop='url']")
        if not link:
            continue
        href = link.get("href", "")
        url = urljoin(AVITO_BASE, href)
        match = re.search(r"_(\d+)$|/(\d+)$", href)
        avito_id = int(match.group(1) or match.group(2)) if match else hash(url) % 10**12
        title_el = card.select_one('[data-marker="item-title"]') or link
        title = title_el.get_text(" ", strip=True)
        price_el = card.select_one('[data-marker="item-price"]')
        price, price_string = _parse_price(price_el.get_text(" ", strip=True) if price_el else None)
        location_el = card.select_one('[data-marker="item-address"]') or card.select_one(
            '[data-marker="item-location"]'
        )
        location = location_el.get_text(" ", strip=True) if location_el else ""
        image_el = card.select_one("img")
        image_url = image_el.get("src", "") if image_el else ""
        listings.append(
            Listing(
                avito_id=avito_id,
                title=title,
                price=price,
                price_string=price_string,
                url=url,
                location=location,
                city=_extract_city(location),
                image_url=image_url,
            )
        )
    return listings


def _generate_demo_listings(product: str, region: str) -> list[Listing]:
    cities = [
        "Махачкала",
        "Дербент",
        "Каспийск",
        "Хасавюрт",
        "Кизляр",
        "Буйнакск",
    ]
    materials = ["деревянная", "металлическая", "садовая", "парковая", "уличная"]
    listings: list[Listing] = []
    base_prices = [3500, 5200, 7800, 12000, 15500, 22000, 28000, 45000]
    now = datetime.utcnow()
    for index in range(42):
        city = cities[index % len(cities)]
        material = materials[index % len(materials)]
        price = base_prices[index % len(base_prices)] + (index % 7) * 500
        avito_id = 8200000000 + index
        listings.append(
            Listing(
                avito_id=avito_id,
                title=f"{product} {material}, {region}, {city}",
                price=price,
                price_string=f"{price:,}".replace(",", " ") + " ₽",
                url=f"{AVITO_BASE}/dagestan/mebel_i_interer/{product.lower()}_{avito_id}",
                location=f"Республика {region}, {city}",
                city=city,
                image_url="",
                posted_at=(now - timedelta(days=index % 14)).isoformat(),
            )
        )
    return listings


class AvitoScraper:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": random.choice(USER_AGENTS),
                "Accept": "application/json, text/html, */*",
                "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
            }
        )
        if config.proxy:
            self.session.proxies.update({"http": config.proxy, "https": config.proxy})

    def _is_blocked(self, payload: Any) -> bool:
        if isinstance(payload, dict) and "too-many-requests" in payload:
            return True
        if isinstance(payload, str):
            lowered = payload.lower()
            return "captcha" in lowered or "ограничен" in lowered
        return False

    def _warmup(self) -> None:
        search_url = (
            f"{AVITO_BASE}/{self.config.region_slug}"
            f"?q={quote(self.config.product)}"
        )
        self.session.get(search_url, timeout=30)

    def _fetch_api_page(self, page: int) -> dict[str, Any]:
        params = {
            "query": self.config.product,
            "locationId": self.config.location_id,
            "page": page,
            "limit": 50,
        }
        response = self.session.get(
            ITEMS_API,
            params=params,
            headers={"Referer": f"{AVITO_BASE}/{self.config.region_slug}"},
            timeout=30,
        )
        if response.status_code in {403, 429}:
            raise AvitoBlockedError(response.text)
        response.raise_for_status()
        data = response.json()
        if self._is_blocked(data):
            raise AvitoBlockedError(json.dumps(data, ensure_ascii=False))
        return data

    def _fetch_html_page(self, page: int) -> str:
        params = {"q": self.config.product}
        if page > 1:
            params["p"] = page
        url = f"{AVITO_BASE}/{self.config.region_slug}"
        response = self.session.get(url, params=params, timeout=30)
        if response.status_code in {403, 429}:
            raise AvitoBlockedError(response.text)
        response.raise_for_status()
        if self._is_blocked(response.text):
            raise AvitoBlockedError("HTML-страница заблокирована")
        return response.text

    def scan(self) -> ScanResult:
        if self.config.demo_mode:
            listings = _generate_demo_listings(self.config.product, self.config.region)
            return ScanResult(
                scan_id=0,
                scanned_at=datetime.utcnow(),
                product=self.config.product,
                region=self.config.region,
                listings=listings,
                total_found=len(listings),
                pages_scanned=1,
                source="demo",
            )

        self._warmup()
        time.sleep(self.config.request_delay_seconds)

        listings: list[Listing] = []
        seen_ids: set[int] = set()
        total_found = 0
        pages_scanned = 0
        source = "api"

        try:
            for page in range(1, self.config.max_pages + 1):
                try:
                    payload = self._fetch_api_page(page)
                except AvitoBlockedError:
                    if listings:
                        break
                    source = "html"
                    html = self._fetch_html_page(page)
                    page_listings = _parse_html_listings(html)
                    if not page_listings:
                        raise
                    for listing in page_listings:
                        if listing.avito_id not in seen_ids:
                            seen_ids.add(listing.avito_id)
                            listings.append(listing)
                    pages_scanned += 1
                    break

                total_found = int(payload.get("totalCount") or payload.get("count") or 0)
                page_items = payload.get("items") or []
                if not page_items:
                    break
                for item in page_items:
                    listing = _item_from_api_payload(item)
                    if listing and listing.avito_id not in seen_ids:
                        seen_ids.add(listing.avito_id)
                        listings.append(listing)
                pages_scanned += 1
                if len(page_items) < 50:
                    break
                time.sleep(self.config.request_delay_seconds)
        except AvitoBlockedError as exc:
            if not listings:
                logger.warning(
                    "Авито заблокировал запросы (%s). Используются демо-данные для отчёта.",
                    exc,
                )
                listings = _generate_demo_listings(self.config.product, self.config.region)
                source = "demo-fallback"
                total_found = len(listings)
                pages_scanned = 1
            else:
                logger.warning("Сканирование прервано из-за блокировки после %s страниц", pages_scanned)

        if not listings:
            raise AvitoScraperError("Не удалось получить объявления с Авито")

        return ScanResult(
            scan_id=0,
            scanned_at=datetime.utcnow(),
            product=self.config.product,
            region=self.config.region,
            listings=listings,
            total_found=total_found or len(listings),
            pages_scanned=pages_scanned,
            source=source,
        )
