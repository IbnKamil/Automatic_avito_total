from __future__ import annotations

import logging
import re
import time
from typing import Callable
from urllib.parse import urljoin

from avito_monitor.config import AppConfig
from avito_monitor.models import Listing
from avito_monitor.scraper import (
    AVITO_BASE,
    _extract_city,
    _extract_total_count_from_html,
    _matches_product,
    _parse_price,
    build_listing_url,
)

logger = logging.getLogger(__name__)


class BrowserScraper:
    """Сканирование через браузер — нужно, когда Авито грузит объявления через JavaScript."""

    def __init__(self, config: AppConfig, search_url_builder: Callable[[int], str]) -> None:
        self.config = config
        self.search_url_builder = search_url_builder

    def scan(self) -> tuple[list[Listing], int, int]:
        try:
            from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise RuntimeError(
                "Для режима браузера установите Playwright:\n"
                "  pip install playwright\n"
                "  playwright install chromium"
            ) from exc

        listings: list[Listing] = []
        seen_ids: set[int] = set()
        total_found = 0
        pages_scanned = 0
        max_pages = self.config.max_pages

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            context_kwargs: dict = {
                "locale": "ru-RU",
                "user_agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
                ),
            }
            if self.config.proxy:
                context_kwargs["proxy"] = {"server": self.config.proxy}
            context = browser.new_context(**context_kwargs)
            page = context.new_page()

            for page_num in range(1, max_pages + 1):
                url = self.search_url_builder(page_num)
                logger.info("Браузер: открываю страницу %s — %s", page_num, url)
                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=60000)
                    page.wait_for_timeout(3000)
                    try:
                        page.wait_for_selector('[data-marker="item"]', timeout=20000)
                    except PlaywrightTimeoutError:
                        logger.warning("Браузер: на странице %s нет карточек объявлений", page_num)
                        if page_num == 1:
                            break
                        break
                except PlaywrightTimeoutError as exc:
                    logger.warning("Браузер: таймаут загрузки страницы %s: %s", page_num, exc)
                    break

                html = page.content()
                if page_num == 1:
                    total_found = _extract_total_count_from_html(html) or total_found
                    if total_found:
                        logger.info("Браузер: на Авито найдено объявлений: %s", total_found)
                    max_pages = min(
                        self.config.max_pages,
                        max(1, (total_found + 49) // 50) if total_found else self.config.max_pages,
                    )

                cards = page.locator('[data-marker="item"]')
                card_count = cards.count()
                if card_count == 0:
                    break

                added = 0
                for index in range(card_count):
                    card = cards.nth(index)
                    try:
                        listing = self._parse_card(card)
                    except Exception as exc:
                        logger.debug("Браузер: пропуск карточки %s: %s", index, exc)
                        continue
                    if not listing or listing.avito_id in seen_ids:
                        continue
                    if not listing.title or not _matches_product(listing.title, self.config.product):
                        continue
                    seen_ids.add(listing.avito_id)
                    listings.append(listing)
                    added += 1

                pages_scanned += 1
                logger.info(
                    "Браузер страница %s: карточек %s, добавлено %s, всего %s",
                    page_num,
                    card_count,
                    added,
                    len(listings),
                )

                if total_found and len(listings) >= total_found:
                    break
                if added == 0:
                    break
                time.sleep(self.config.request_delay_seconds)

            browser.close()

        return listings, total_found or len(listings), pages_scanned

    def _parse_card(self, card) -> Listing | None:
        link = card.locator('[data-marker="item-title"]').first
        if link.count() == 0:
            link = card.locator("a[itemprop='url']").first
        if link.count() == 0:
            return None

        href = link.get_attribute("href") or ""
        if not href:
            return None
        match = re.search(r"_(\d{6,})$", href) or re.search(r"/(\d{6,})(?:\?|$)", href)
        if not match:
            item_id = card.get_attribute("data-item-id")
            if not item_id:
                return None
            avito_id = int(item_id)
        else:
            avito_id = int(match.group(1))

        title = link.inner_text().strip()
        price_text = ""
        price_locator = card.locator('[data-marker="item-price"]')
        if price_locator.count():
            price_text = price_locator.first.inner_text().strip()
        price, price_string = _parse_price(price_text or None)

        location = ""
        for selector in ('[data-marker="item-address"]', '[data-marker="item-location"]'):
            loc = card.locator(selector)
            if loc.count():
                location = loc.first.inner_text().strip()
                break

        url = urljoin(AVITO_BASE, href.split("?", 1)[0])
        return Listing(
            avito_id=avito_id,
            title=title,
            price=price,
            price_string=price_string,
            url=url,
            location=location,
            city=_extract_city(location),
            image_url="",
        )
