from __future__ import annotations

import csv
import json
import logging
import random
import re
import time
from collections import Counter
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
        if amount is not None and amount <= 0:
            return None, label or "Цена не указана"
        return (int(amount) if amount is not None else None), label or "Цена не указана"
    if isinstance(value, (int, float)):
        amount = int(value)
        if amount <= 0:
            return None, "Цена не указана"
        return amount, f"{amount:,}".replace(",", " ") + " ₽"
    if isinstance(value, str):
        digits = re.sub(r"[^\d]", "", value)
        amount = int(digits) if digits else None
        if amount is not None and amount <= 0:
            return None, value or "Цена не указана"
        return amount, value
    return None, "Цена не указана"


def _product_keywords(product: str) -> set[str]:
    product_lower = product.lower().strip()
    keywords = set()
    if not product_lower:
        return keywords
    keywords.update(
        {
            product_lower,
            product_lower.rstrip("и"),
            product_lower.rstrip("ы"),
            product_lower.rstrip("а"),
        }
    )
    if "скамей" in product_lower or product_lower.startswith("скам"):
        keywords.update({"скамей", "скамейк", "скамья", "лавка", "садовая лавка"})
    return {kw for kw in keywords if kw}


def _matches_product(title: str, product: str) -> bool:
    title_lower = title.lower()
    keywords = _product_keywords(product)
    if not keywords:
        return True
    return any(keyword in title_lower for keyword in keywords)


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
    )
    for pattern in patterns:
        match = re.search(pattern, html, flags=re.IGNORECASE)
        if match:
            digits = re.sub(r"\D", "", match.group(1))
            if digits:
                value = int(digits)
                if value >= 1:
                    return value
    return None


def _extract_total_count_from_api_payload(payload: dict[str, Any]) -> int | None:
    for key in ("totalCount", "foundCount", "count"):
        value = payload.get(key)
        if value is not None:
            try:
                return int(value)
            except (TypeError, ValueError):
                continue
    return None


def _extract_search_context(html: str) -> str | None:
    candidates: list[str] = []
    for text in (html, html.replace('\\"', '"'), bytes(html, "utf-8").decode("unicode_escape", errors="ignore")):
        contexts = re.findall(r"context=(H4sI[A-Za-z0-9+/=_\-]+)", text)
        candidates.extend(contexts)
        for pattern in (
            r'"context"\s*:\s*"(H4sI[^"]+)"',
            r'"searchHash"\s*:\s*"([^"]+)"',
        ):
            match = re.search(pattern, text)
            if match:
                candidates.append(match.group(1))
    if candidates:
        return Counter(candidates).most_common(1)[0][0]
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
        r'window\.__preloadedState__\s*=\s*"(.+?)"\s*;',
        r'window\.__preloadedState__\s*=\s*"(.+?)";\s*window',
        r"window\.__staticRouterHydrationData\s*=\s*JSON\.parse\(\"(.+?)\"\);",
        r"window\.__mfe__\s*=\s*\"(.+?)\";",
        r'<script[^>]*type="application/json"[^>]*>(.*?)</script>',
    )
    for pattern in script_patterns:
        for match in re.finditer(pattern, html, flags=re.DOTALL):
            parsed = _decode_embedded_json_snippet(match.group(1))
            if parsed is not None:
                blobs.append(parsed)
    return blobs


def _parse_items_from_url_paths(html: str, config: AppConfig) -> list[Listing]:
    listings: list[Listing] = []
    seen: set[int] = set()
    for text in (html, html.replace('\\"', '"')):
        for match in re.finditer(
            r'"urlPath"\s*:\s*"(/(?:[^"\\]|\\.)+)"\s*,\s*"[^"]*"\s*:\s*"[^"]*"\s*,\s*"title"\s*:\s*"(?P<title>(?:\\.|[^"\\])*)"',
            text,
        ):
            url_path = json.loads(f'"{match.group(1)}"')
            title = json.loads(f'"{match.group("title")}"')
            id_match = re.search(r"_(\d{6,})(?:\?|$)", url_path)
            if not id_match:
                continue
            avito_id = int(id_match.group(1))
            if avito_id in seen:
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
        for match in re.finditer(r'href="(/[^"]+_\d{6,})(?:\?[^"]*)?"', text):
            href = match.group(1)
            id_match = re.search(r"_(\d{6,})$", href)
            if not id_match:
                continue
            avito_id = int(id_match.group(1))
            if avito_id in seen:
                continue
            seen.add(avito_id)
            listings.append(
                Listing(
                    avito_id=avito_id,
                    title="",
                    price=None,
                    price_string="Цена не указана",
                    url=build_listing_url(href, avito_id, config.region_slug),
                    location="",
                    city="Не указан",
                    image_url="",
                    raw={"urlPath": href},
                )
            )
    return listings


def _extract_catalog_items_from_state(blob: Any) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    if not isinstance(blob, dict):
        return items
    loader = blob.get("loaderData")
    if not isinstance(loader, dict):
        return items
    catalog_block = loader.get("catalog-or-main-or-item")
    if not isinstance(catalog_block, dict):
        return items
    for key in ("items", "catalogItems", "list"):
        value = catalog_block.get(key)
        if isinstance(value, list):
            items.extend(item for item in value if isinstance(item, dict))
    catalog = catalog_block.get("catalog")
    if isinstance(catalog, dict) and isinstance(catalog.get("items"), list):
        items.extend(item for item in catalog["items"] if isinstance(item, dict))
    return items


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
    seen: set[int] = set()
    cards = soup.select(
        '[data-marker="item"], [data-item-id], [itemtype="http://schema.org/Product"]'
    )
    for card in cards:
        link = card.select_one('[data-marker="item-title"]')
        if not link:
            link = card.select_one("a[itemprop='url']")
        if not link:
            link = card.select_one("a[href]")
        if not link:
            continue
        href = link.get("href", "")
        if not href:
            continue
        url = urljoin(AVITO_BASE, href.split("?", 1)[0])
        match = re.search(r"_(\d{6,})$", href)
        if not match:
            match = re.search(r"/(\d{6,})(?:\?|$)", href)
        if not match and card.get("data-item-id"):
            avito_id = int(card["data-item-id"])
        elif match:
            avito_id = int(match.group(1))
        else:
            continue
        if avito_id in seen:
            continue
        seen.add(avito_id)
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

    if not listings:
        for link in soup.select('a[href*="_"]'):
            href = link.get("href", "")
            match = re.search(r"_(\d{6,})$", href)
            if not match:
                continue
            avito_id = int(match.group(1))
            if avito_id in seen:
                continue
            title = link.get_text(" ", strip=True)
            if not title:
                continue
            seen.add(avito_id)
            listings.append(
                Listing(
                    avito_id=avito_id,
                    title=title,
                    price=None,
                    price_string="Цена не указана",
                    url=urljoin(AVITO_BASE, href.split("?", 1)[0]),
                    location="",
                    city="Не указан",
                    image_url="",
                )
            )
    return listings


def _parse_page_listings(html: str, config: AppConfig) -> list[Listing]:
    parsers = (
        _parse_html_listings,
        _parse_json_items_from_html,
        _parse_items_from_url_paths,
    )
    combined: list[Listing] = []
    seen: set[int] = set()
    for parser in parsers:
        for listing in parser(html, config):
            if listing.avito_id not in seen:
                seen.add(listing.avito_id)
                combined.append(listing)
    return combined


def _parse_json_items_from_html(html: str, config: AppConfig) -> list[Listing]:
    listings: list[Listing] = []
    seen: set[int] = set()

    for blob in _extract_json_blobs_from_html(html):
        catalog_items = _extract_catalog_items_from_state(blob)
        source_items = catalog_items or _walk_item_dicts(blob)
        for item in source_items:
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
        fast_fail = self.config.use_browser or self.config.browser_only
        max_attempts = 1 if fast_fail else 3
        backoff = 0.0 if fast_fail else self.config.rate_limit_backoff_seconds
        for attempt in range(max_attempts):
            response = self.session.request(method, url, **kwargs)
            if response.status_code not in {429, 503}:
                return response
            last_error = AvitoBlockedError(response.text)
            if attempt + 1 >= max_attempts:
                break
            wait_seconds = backoff * (attempt + 1)
            logger.warning(
                "Авито вернул %s, повтор через %s сек.",
                response.status_code,
                wait_seconds,
            )
            time.sleep(wait_seconds)
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
        if self._search_context:
            logger.info("Контекст поиска Авито получен")
        else:
            logger.warning("Контекст поиска не найден — API может вернуть нерелевантные объявления")
        if self._catalog_total_count:
            logger.info("На Авито найдено объявлений: %s", self._catalog_total_count)
        product_hits = html.lower().count(self.config.product.lower().rstrip("и"))
        logger.info(
            "HTML страница поиска: %s символов, упоминаний товара: %s",
            len(html),
            product_hits,
        )
        return html

    def _accept_listing(self, listing: Listing) -> bool:
        if not listing.title:
            return False
        return _matches_product(listing.title, self.config.product)

    def _api_items_are_relevant(self, items: list[dict[str, Any]]) -> bool:
        if not items:
            return False
        sample = items[:20]
        matched = sum(
            1
            for item in sample
            if _matches_product(str(item.get("title") or ""), self.config.product)
        )
        ratio = matched / len(sample)
        logger.info(
            "Проверка релевантности API: %s/%s объявлений по запросу «%s» (%.0f%%)",
            matched,
            len(sample),
            self.config.product,
            ratio * 100,
        )
        return ratio >= 0.4

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

    def _parse_page(self, html: str) -> list[Listing]:
        return _parse_page_listings(html, self.config)

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
            "display": "list",
            "lastStamp": 0,
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

    def _scan_api_source(
        self,
        fetch_name: str,
        fetch_page: Any,
        listings: list[Listing],
        seen_ids: set[int],
        total_found: int,
    ) -> tuple[int, int, str]:
        first_payload = fetch_page(1)
        first_items = first_payload.get("items") or []
        if not self._api_items_are_relevant(first_items):
            logger.warning("%s: объявления не соответствуют запросу, пропускаем", fetch_name)
            return total_found, 0, ""

        api_total = int(
            first_payload.get("totalCount")
            or first_payload.get("foundCount")
            or first_payload.get("count")
            or total_found
            or 0
        )
        if api_total:
            total_found = max(total_found, api_total)
        max_pages = self._target_page_count(total_found)
        api_pages = 0

        for page in range(1, max_pages + 1):
            payload = first_payload if page == 1 else fetch_page(page)
            page_items = payload.get("items") or []
            if not page_items:
                logger.info("%s страница %s: пустой ответ, остановка", fetch_name, page)
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
            if total_found and len(listings) >= total_found:
                break
            if added == 0:
                logger.info("%s страница %s: новых релевантных объявлений нет", fetch_name, page)
                break
            time.sleep(self.config.request_delay_seconds)

        return total_found, api_pages, fetch_name if api_pages else ""

    def _scan_catalog(self) -> tuple[list[Listing], int, int, str]:
        listings: list[Listing] = []
        seen_ids: set[int] = set()
        pages_scanned = 0
        source = "html"
        bootstrap_html = self._bootstrap_search()
        total_found = self._catalog_total_count or 0
        max_pages = self._target_page_count(total_found) if total_found else self.config.max_pages

        for page in range(1, max_pages + 1):
            html = bootstrap_html if page == 1 else self._fetch_html_page(page)
            page_listings = self._parse_page(html)
            if not page_listings:
                logger.info("HTML страница %s: объявлений не найдено", page)
                if page == 1:
                    break
                break
            added = self._ingest_page_listings(page_listings, listings, seen_ids)
            pages_scanned += 1
            source = "html"
            logger.info(
                "HTML страница %s: найдено %s, добавлено %s (релевантных), всего %s",
                page,
                len(page_listings),
                added,
                len(listings),
            )
            if total_found and len(listings) >= total_found:
                break
            if added == 0:
                break
            time.sleep(self.config.request_delay_seconds)

        need_more = not listings or (total_found and len(listings) < total_found)
        if need_more:
            time.sleep(max(self.config.request_delay_seconds, 5))
            for fetch_name, fetch_page in (("api", self._fetch_api_page), ("js-api", self._fetch_js_api_page)):
                try:
                    total_found, api_pages, used_source = self._scan_api_source(
                        fetch_name, fetch_page, listings, seen_ids, total_found
                    )
                    if api_pages:
                        pages_scanned = max(pages_scanned, api_pages)
                        source = used_source
                        break
                except AvitoBlockedError as exc:
                    logger.warning("%s недоступен: %s", fetch_name, exc)

        if not listings:
            try:
                for page in range(1, self.config.max_pages + 1):
                    page_items = self._fetch_mobile_page(page)
                    if not page_items or not self._api_items_are_relevant(page_items):
                        break
                    added = self._ingest_api_items(page_items, listings, seen_ids)
                    pages_scanned += 1
                    source = "mobile-api"
                    if not added:
                        break
                    time.sleep(self.config.request_delay_seconds)
            except AvitoBlockedError as exc:
                logger.warning("mobile-api недоступен: %s", exc)

        return listings, total_found or len(listings), pages_scanned, source

    def _try_browser_scan(
        self,
    ) -> tuple[list[Listing], int, int, str] | None:
        logger.info("Запуск браузерного сканирования Playwright...")
        try:
            from avito_monitor.browser_scraper import BrowserScraper

            browser_listings, browser_total, browser_pages = BrowserScraper(
                self.config, self._search_url
            ).scan()
            if browser_listings:
                logger.info(
                    "Браузер: собрано %s из %s объявлений",
                    len(browser_listings),
                    browser_total,
                )
                return browser_listings, browser_total, browser_pages, "browser"
            logger.warning("Браузер: релевантные объявления не найдены")
        except RuntimeError as exc:
            logger.error("%s", exc)
            if self.config.browser_only:
                raise AvitoScraperError(str(exc)) from exc
        except Exception as exc:
            logger.warning("Браузерное сканирование не удалось: %s", exc)
        return None

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

        if self.config.use_browser or self.config.browser_only:
            browser_result = self._try_browser_scan()
            if browser_result:
                listings, total_found, pages_scanned, source = browser_result
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
            if self.config.browser_only or self.config.use_browser:
                raise AvitoScraperError(
                    f"Браузерное сканирование не дало результатов для «{self.config.product}» "
                    f"в регионе «{self.config.region}».\n"
                    f"Скорее всего Авито заблокировал IP или показал капчу.\n\n"
                    f"Попробуйте:\n"
                    f"  1. git pull origin cursor/avito-bench-monitor-b4b9\n"
                    f"  2. python -m avito_monitor scan --visible-browser --no-email\n"
                    f"     (откроется окно Chrome — пройдите капчу если появится)\n"
                    f"  3. Подождите 30–60 мин и повторите\n"
                    f"  4. Смените интернет или укажите PROXY в .env\n\n"
                    f"Диагностика сохраняется в reports/debug/\n"
                    f"Проверка вручную: {self._search_url(1)}"
                )

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
        elif last_block_error and not listings:
            raise AvitoScraperError(
                "Авито заблокировал запросы. Попробуйте позже, запустите с другого интернета "
                "или укажите PROXY в .env. Для обхода блокировки включите браузерный режим:\n"
                "  USE_BROWSER=true\n"
                "  pip install playwright\n"
                "  python -m playwright install chromium\n"
                "Для теста отчёта без Авито: python -m avito_monitor scan --demo --no-email"
            ) from last_block_error

        if not listings:
            raise AvitoScraperError(
                f"Не найдено объявлений «{self.config.product}» в регионе «{self.config.region}». "
                f"Авито ограничивает частые запросы (429). Включите браузерный режим:\n"
                f"  USE_BROWSER=true\n"
                f"  pip install playwright\n"
                f"  python -m playwright install chromium\n"
                f"Или запустите: python -m avito_monitor scan --browser-only --no-email\n"
                f"Проверка в браузере: {self._search_url(1)}"
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
