from __future__ import annotations

import csv
import json
import logging
import random
import re
import time
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import quote, urljoin, urlparse

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
JS_ITEMS_API = "https://www.avito.ru/web/1/js/items"
MOBILE_ITEMS_API = "https://www.avito.ru/api/9/items"
MOBILE_API_KEY = "ZaeC8aidairahqu2Eeb1quee9einaeFieboocohX"
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


def build_listing_url(url_path: str | None, avito_id: int, region_slug: str) -> str:
    if url_path:
        path = url_path.split("?", 1)[0]
        if not path.startswith("/"):
            path = f"/{path}"
        return urljoin(AVITO_BASE, path)
    return f"{AVITO_BASE}/{region_slug}?q={avito_id}"


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


def _normalize_location(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        for key in (
            "formattedAddress",
            "name",
            "title",
            "address",
            "value",
            "text",
            "city",
            "region",
        ):
            nested = value.get(key)
            if nested:
                if isinstance(nested, str):
                    return nested.strip()
                if isinstance(nested, dict):
                    normalized = _normalize_location(nested)
                    if normalized:
                        return normalized
        string_values = [str(item).strip() for item in value.values() if item]
        return ", ".join(string_values[:3])
    if isinstance(value, list):
        parts = [_normalize_location(item) for item in value]
        return ", ".join(part for part in parts if part)
    return str(value).strip()


def _extract_city(location: Any) -> str:
    location_text = _normalize_location(location)
    if not location_text:
        return "Не указан"
    parts = [part.strip() for part in location_text.split(",") if part.strip()]
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


def _matches_product(title: str, product: str) -> bool:
    title_lower = title.lower()
    product_lower = product.lower().strip()
    if not product_lower:
        return True
    if product_lower in title_lower:
        return True
    stems = {
        product_lower,
        product_lower.rstrip("и"),
        product_lower.rstrip("ы"),
        product_lower[: max(5, len(product_lower) - 2)],
    }
    return any(stem and stem in title_lower for stem in stems if stem)


def _is_html_blocked(html: str) -> bool:
    lowered = html.lower()
    if len(html) < 8000 and (
        "доступ с вашего ip-адреса временно ограничен" in lowered
        or "too-many-requests" in lowered
    ):
        return True
    block_markers = (
        "доступ с вашего ip-адреса временно ограничен",
        "firewall/captcha/show",
        "подтвердите, что вы не робот",
        "data-marker=\"captcha\"",
        "data-marker='captcha'",
        "checkpoint-captcha",
    )
    return any(marker in lowered for marker in block_markers)


def _is_api_blocked(payload: Any) -> bool:
    if isinstance(payload, dict):
        return "too-many-requests" in payload
    return False


def _walk_item_dicts(payload: Any, found: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    if found is None:
        found = []
    if isinstance(payload, dict):
        item_id = payload.get("id")
        title = payload.get("title")
        url_path = payload.get("urlPath") or payload.get("url")
        if item_id and title and url_path:
            found.append(payload)
        for value in payload.values():
            _walk_item_dicts(value, found)
    elif isinstance(payload, list):
        for value in payload:
            _walk_item_dicts(value, found)
    return found


def _extract_total_count_from_html(html: str) -> int | None:
    patterns = (
        r"(\d[\d\s\xa0]+)\s+объявлен",
        r'"totalCount"\s*:\s*(\d+)',
        r'"foundCount"\s*:\s*(\d+)',
        r'"count"\s*:\s*(\d+)',
    )
    for pattern in patterns:
        match = re.search(pattern, html, flags=re.IGNORECASE)
        if match:
            digits = re.sub(r"\D", "", match.group(1))
            if digits:
                return int(digits)
    return None


def _extract_search_context(html: str) -> str | None:
    patterns = (
        r'"context"\s*:\s*"(H4sI[^"]+)"',
        r'"searchHash"\s*:\s*"([^"]+)"',
        r'context=([A-Za-z0-9_\-]+)',
    )
    for pattern in patterns:
        match = re.search(pattern, html)
        if match:
            return match.group(1)
    return None


def _decode_embedded_json_snippet(raw: str) -> Any | None:
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        try:
            return json.loads(raw.encode("utf-8").decode("unicode_escape"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None


def _extract_json_blobs_from_html(html: str) -> list[Any]:
    blobs: list[Any] = []
    script_patterns = (
        r'window\.__preloadedState__\s*=\s*"(.+?)";\s*window',
        r"window\.__staticRouterHydrationData\s*=\s*JSON\.parse\(\"(.+?)\"\);",
        r'<script[^>]*type="application/json"[^>]*>(.*?)</script>',
    )
    for pattern in script_patterns:
        for match in re.finditer(pattern, html, flags=re.DOTALL):
            parsed = _decode_embedded_json_snippet(match.group(1))
            if parsed is not None:
                blobs.append(parsed)
    return blobs


def _listing_from_raw_item(item: dict[str, Any], config: AppConfig) -> Listing | None:
    if item.get("urlPath") or item.get("url"):
        return _item_from_api_payload(item, config)
    return _item_from_mobile_payload(item, config)


def _matches_region(
    listing: Listing, region_slug: str, location_id: int, region_name: str
) -> bool:
    location_lower = listing.location.lower()
    region_name_lower = region_name.lower()
    if region_name_lower in location_lower or "дагестан" in location_lower:
        return True
    url_path = urlparse(listing.url).path.lower()
    if f"/{region_slug.lower()}/" in url_path:
        return True
    raw_location_id = listing.raw.get("locationId")
    if raw_location_id and int(raw_location_id) == location_id:
        return True
    foreign_markers = (
        "москва",
        "санкт-петербург",
        "спб",
        "краснодар",
        "новосибирск",
        "екатеринбург",
        "саратов",
        "ростов",
    )
    if any(marker in location_lower for marker in foreign_markers):
        return False
    return True


def _item_from_mobile_payload(item: dict[str, Any], config: AppConfig) -> Listing | None:
    avito_id = item.get("id")
    if not avito_id:
        return None
    title = item.get("title") or "Без названия"
    price, price_string = _parse_price(item.get("price") or item.get("priceDetailed"))
    location = _normalize_location(item.get("address") or item.get("location"))
    url_value = item.get("url") or item.get("uri") or item.get("urlPath") or ""
    if url_value and not str(url_value).startswith("http"):
        url = build_listing_url(str(url_value), int(avito_id), config.region_slug)
    else:
        url = str(url_value) if url_value else build_listing_url(None, int(avito_id), config.region_slug)
    images = item.get("images") or []
    image_url = ""
    if images and isinstance(images[0], dict):
        image_url = next(iter(images[0].values()), "")
    return Listing(
        avito_id=int(avito_id),
        title=title.strip(),
        price=price,
        price_string=price_string,
        url=url,
        location=location,
        city=_extract_city(location),
        image_url=image_url,
        posted_at=None,
        raw=item,
    )


def _item_from_api_payload(item: dict[str, Any], config: AppConfig) -> Listing | None:
    avito_id = item.get("id")
    if not avito_id:
        return None
    title = item.get("title") or item.get("name") or "Без названия"
    price, price_string = _parse_price(
        item.get("priceDetailed") or item.get("price") or item.get("priceValue")
    )
    geo = item.get("geo") or {}
    location = _normalize_location(
        geo.get("formattedAddress")
        or geo.get("address")
        or item.get("location")
        or item.get("address")
    )
    url = build_listing_url(item.get("urlPath") or item.get("url"), int(avito_id), config.region_slug)
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


def _parse_html_listings(html: str, config: AppConfig) -> list[Listing]:
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
        if not href:
            continue
        url = urljoin(AVITO_BASE, href.split("?", 1)[0])
        match = re.search(r"_(\d{6,})$", href)
        if not match:
            match = re.search(r"/(\d{6,})(?:\?|$)", href)
        if not match:
            continue
        avito_id = int(match.group(1))
        title_el = card.select_one('[data-marker="item-title"]') or link
        title = title_el.get_text(" ", strip=True)
        price_el = card.select_one('[data-marker="item-price"]')
        price, price_string = _parse_price(
            price_el.get_text(" ", strip=True) if price_el else None
        )
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


def _parse_json_items_from_html(html: str, config: AppConfig) -> list[Listing]:
    listings: list[Listing] = []
    seen: set[int] = set()

    for blob in _extract_json_blobs_from_html(html):
        for item in _walk_item_dicts(blob):
            listing = _listing_from_raw_item(item, config)
            if listing and listing.avito_id not in seen:
                seen.add(listing.avito_id)
                listings.append(listing)

    if listings:
        return listings

    pattern = re.compile(
        r'"id"\s*:\s*(\d{6,})\s*,\s*'
        r'(?:"urlPath"\s*:\s*"(?P<urlPath>[^"]+)"\s*,\s*)?'
        r'"title"\s*:\s*"(?P<title>(?:\\.|[^"\\])*)"',
        re.DOTALL,
    )
    for match in pattern.finditer(html):
        avito_id = int(match.group(1))
        if avito_id in seen:
            continue
        title = json.loads(f'"{match.group("title")}"')
        url_path = match.group("urlPath")
        if not url_path:
            continue
        seen.add(avito_id)
        listings.append(
            Listing(
                avito_id=avito_id,
                title=title.strip(),
                price=None,
                price_string="Цена не указана",
                url=build_listing_url(url_path, avito_id, config.region_slug),
                location="",
                city="Не указан",
                image_url="",
                raw={"urlPath": url_path},
            )
        )
    return listings


def _generate_demo_listings(product: str, region: str, region_slug: str) -> list[Listing]:
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
    for index in range(10):
        city = cities[index % len(cities)]
        material = materials[index % len(materials)]
        price = base_prices[index % len(base_prices)] + (index % 7) * 500
        listings.append(
            Listing(
                avito_id=0,
                title=f"[ДЕМО] {product} {material}, {region}, {city}",
                price=price,
                price_string=f"{price:,}".replace(",", " ") + " ₽",
                url="",
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
        self._search_context: str | None = None
        self._catalog_total_count: int | None = None

    def _search_url(self, page: int = 1) -> str:
        params = [f"q={quote(self.config.product)}", "localPriority=1"]
        if page > 1:
            params.append(f"p={page}")
        return f"{AVITO_BASE}/{self.config.region_slug}?{'&'.join(params)}"

    def _search_params(self, page: int = 1) -> dict[str, Any]:
        params: dict[str, Any] = {
            "q": self.config.product,
            "localPriority": 1,
        }
        if page > 1:
            params["p"] = page
        return params

    def _request_with_retry(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        last_error: Exception | None = None
        for attempt in range(3):
            response = self.session.request(method, url, **kwargs)
            if response.status_code not in {429, 503}:
                return response
            last_error = AvitoBlockedError(response.text)
            logger.warning("Авито вернул %s, повтор через %s сек.", response.status_code, 2 + attempt)
            time.sleep(2 + attempt)
        if last_error:
            raise last_error
        raise AvitoBlockedError("Превышено число попыток запроса к Авито")

    def _bootstrap_search(self) -> str:
        response = self._request_with_retry(
            "GET",
            f"{AVITO_BASE}/{self.config.region_slug}",
            params=self._search_params(1),
            headers={"Referer": AVITO_BASE},
            timeout=30,
        )
        if response.status_code == 403:
            raise AvitoBlockedError(response.text)
        response.raise_for_status()
        html = response.text
        if _is_html_blocked(html):
            raise AvitoBlockedError("HTML-страница заблокирована")
        self._search_context = _extract_search_context(html)
        self._catalog_total_count = _extract_total_count_from_html(html)
        if self._catalog_total_count:
            logger.info("На Авито найдено объявлений: %s", self._catalog_total_count)
        return html

    def _accept_listing(self, listing: Listing) -> bool:
        return True

    def _add_listing(
        self,
        listing: Listing | None,
        listings: list[Listing],
        seen_ids: set[int],
    ) -> bool:
        if not listing or listing.avito_id in seen_ids:
            return False
        if not self._accept_listing(listing):
            return False
        seen_ids.add(listing.avito_id)
        listings.append(listing)
        return True

    def _parse_page_listings(self, html: str) -> list[Listing]:
        page_listings = _parse_html_listings(html, self.config)
        if page_listings:
            return page_listings
        return _parse_json_items_from_html(html, self.config)

    def _fetch_html_page(self, page: int) -> str:
        response = self._request_with_retry(
            "GET",
            f"{AVITO_BASE}/{self.config.region_slug}",
            params=self._search_params(page),
            headers={"Referer": self._search_url(max(1, page - 1))},
            timeout=30,
        )
        if response.status_code == 403:
            raise AvitoBlockedError(response.text)
        response.raise_for_status()
        if _is_html_blocked(response.text):
            raise AvitoBlockedError("HTML-страница заблокирована")
        return response.text

    def _api_params(self, page: int) -> dict[str, Any]:
        params: dict[str, Any] = {
            "query": self.config.product,
            "q": self.config.product,
            "locationId": self.config.location_id,
            "localPriority": 1,
            "page": page,
            "limit": 50,
        }
        if self._search_context:
            params["context"] = self._search_context
        return params

    def _fetch_api_page(self, page: int) -> dict[str, Any]:
        response = self._request_with_retry(
            "GET",
            ITEMS_API,
            params=self._api_params(page),
            headers={"Referer": self._search_url(page)},
            timeout=30,
        )
        if response.status_code == 403:
            raise AvitoBlockedError(response.text)
        response.raise_for_status()
        data = response.json()
        if _is_api_blocked(data):
            raise AvitoBlockedError(json.dumps(data, ensure_ascii=False))
        return data

    def _fetch_js_api_page(self, page: int) -> dict[str, Any]:
        response = self._request_with_retry(
            "GET",
            JS_ITEMS_API,
            params=self._api_params(page),
            headers={"Referer": self._search_url(page)},
            timeout=30,
        )
        if response.status_code == 403:
            raise AvitoBlockedError(response.text)
        response.raise_for_status()
        data = response.json()
        if _is_api_blocked(data):
            raise AvitoBlockedError(json.dumps(data, ensure_ascii=False))
        return data

    def _fetch_mobile_page(self, page: int) -> list[dict[str, Any]]:
        params = {
            "query": self.config.product,
            "locationId": self.config.location_id,
            "page": page,
            "key": MOBILE_API_KEY,
            "localPriority": 1,
        }
        response = self._request_with_retry(
            "GET",
            MOBILE_ITEMS_API,
            params=params,
            headers={"User-Agent": "AVITO 52.0 (iPad5,1; 11.3.1; ru_RU)"},
            timeout=30,
        )
        if response.status_code == 403:
            raise AvitoBlockedError(response.text)
        response.raise_for_status()
        data = response.json()
        if _is_api_blocked(data):
            raise AvitoBlockedError(json.dumps(data, ensure_ascii=False))
        result = data.get("result", data)
        items = result.get("items") if isinstance(result, dict) else None
        return items or []

    def _ingest_page_listings(
        self,
        page_listings: list[Listing],
        listings: list[Listing],
        seen_ids: set[int],
    ) -> int:
        added = 0
        for listing in page_listings:
            if self._add_listing(listing, listings, seen_ids):
                added += 1
        return added

    def _ingest_api_items(
        self,
        page_items: list[dict[str, Any]],
        listings: list[Listing],
        seen_ids: set[int],
    ) -> int:
        added = 0
        for item in page_items:
            try:
                listing = _listing_from_raw_item(item, self.config)
                if self._add_listing(listing, listings, seen_ids):
                    added += 1
            except Exception as exc:
                logger.warning("Пропущено объявление из API: %s", exc)
        return added

    def _target_page_count(self, total_found: int) -> int:
        if total_found <= 0:
            return self.config.max_pages
        return min(self.config.max_pages, max(1, (total_found + 49) // 50))

    def _scan_catalog(self) -> tuple[list[Listing], int, int, str]:
        listings: list[Listing] = []
        seen_ids: set[int] = set()
        pages_scanned = 0
        total_found = 0
        source = "html"

        bootstrap_html = self._bootstrap_search()
        first_page_listings = self._parse_page_listings(bootstrap_html)
        added = self._ingest_page_listings(first_page_listings, listings, seen_ids)
        if first_page_listings:
            pages_scanned = 1
            source = "html"
            logger.info("HTML/JSON страница 1: найдено %s, добавлено %s", len(first_page_listings), added)

        total_found = self._catalog_total_count or len(listings)

        for fetch_name, fetch_page in (("api", self._fetch_api_page), ("js-api", self._fetch_js_api_page)):
            if total_found and len(listings) >= total_found:
                break
            try:
                api_pages = 0
                api_total = total_found
                start_page = 2 if pages_scanned >= 1 else 1
                for page in range(start_page, self._target_page_count(total_found) + 1):
                    payload = fetch_page(page)
                    api_total = int(
                        payload.get("totalCount")
                        or payload.get("foundCount")
                        or payload.get("count")
                        or api_total
                        or 0
                    )
                    page_items = payload.get("items") or []
                    if not page_items:
                        break
                    added = self._ingest_api_items(page_items, listings, seen_ids)
                    api_pages += 1
                    logger.info(
                        "%s страница %s: элементов %s, добавлено %s, всего %s",
                        fetch_name,
                        page,
                        len(page_items),
                        added,
                        len(listings),
                    )
                    if api_total:
                        total_found = max(total_found, api_total)
                    if len(page_items) < 50:
                        break
                    if total_found and len(listings) >= total_found:
                        break
                    time.sleep(self.config.request_delay_seconds)
                if listings and api_pages:
                    pages_scanned = max(pages_scanned, api_pages)
                    source = fetch_name
                    break
            except AvitoBlockedError as exc:
                logger.warning("%s недоступен: %s", fetch_name, exc)

        if total_found and len(listings) < total_found:
            for page in range(2, self._target_page_count(total_found) + 1):
                if len(listings) >= total_found:
                    break
                try:
                    html = self._fetch_html_page(page)
                    page_listings = self._parse_page_listings(html)
                    if not page_listings:
                        break
                    added = self._ingest_page_listings(page_listings, listings, seen_ids)
                    pages_scanned += 1
                    logger.info("HTML страница %s: найдено %s, добавлено %s", page, len(page_listings), added)
                    if len(page_listings) < 30:
                        break
                    time.sleep(self.config.request_delay_seconds)
                except AvitoBlockedError:
                    break

        if not listings:
            for page in range(1, self.config.max_pages + 1):
                page_items = self._fetch_mobile_page(page)
                if not page_items:
                    break
                added = self._ingest_api_items(page_items, listings, seen_ids)
                pages_scanned += 1
                source = "mobile-api"
                if added == 0 or len(page_items) < 30:
                    break
                time.sleep(self.config.request_delay_seconds)

        return listings, total_found or len(listings), pages_scanned, source

    def scan(self) -> ScanResult:
        if self.config.demo_mode:
            listings = _generate_demo_listings(
                self.config.product, self.config.region, self.config.region_slug
            )
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

        listings: list[Listing] = []
        total_found = 0
        pages_scanned = 0
        source = "html"
        last_block_error: AvitoBlockedError | None = None

        try:
            listings, total_found, pages_scanned, source = self._scan_catalog()
        except AvitoBlockedError as exc:
            last_block_error = exc

        if listings:
            logger.info(
                "Сканирование завершено: собрано %s из %s объявлений (%s)",
                len(listings),
                total_found or len(listings),
                source,
            )
        elif last_block_error:
            raise AvitoScraperError(
                "Авито заблокировал все способы получения объявлений. "
                "Попробуйте позже, запустите с другого интернета или укажите PROXY в .env. "
                "Для теста отчёта без Авито: python -m avito_monitor scan --demo --no-email"
            ) from last_block_error

        if not listings:
            raise AvitoScraperError(
                f"Не найдено объявлений по запросу «{self.config.product}» "
                f"в регионе «{self.config.region}». "
                f"Проверьте настройки и убедитесь, что на Авито есть результаты: "
                f"{self._search_url(1)}"
            )

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


def export_listings_csv(listings: list[Listing], path: str) -> None:
    rows = sorted(
        listings,
        key=lambda item: (item.price is None, item.price or 0, item.title),
    )
    with open(path, "w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["avito_id", "title", "price", "price_string", "city", "location", "url"],
        )
        writer.writeheader()
        for listing in rows:
            writer.writerow(
                {
                    "avito_id": listing.avito_id,
                    "title": listing.title,
                    "price": listing.price if listing.price is not None else "",
                    "price_string": listing.price_string,
                    "city": listing.city,
                    "location": listing.location,
                    "url": listing.url,
                }
            )
