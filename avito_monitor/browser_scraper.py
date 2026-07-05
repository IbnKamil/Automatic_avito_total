from __future__ import annotations

import logging
import re
import time
from typing import Any, Callable
from urllib.parse import urljoin

from avito_monitor.config import AppConfig, resolve_project_path
from avito_monitor.models import Listing
from avito_monitor.scraper import (
    AVITO_BASE,
    _extract_city,
    _extract_total_count_from_api_payload,
    _extract_total_count_from_html,
    _item_from_api_payload,
    _matches_product,
    _parse_page_listings,
    _parse_price,
)

logger = logging.getLogger(__name__)

_BLOCK_MARKERS = (
    "доступ с вашего ip-адреса временно ограничен",
    "firewall/captcha",
    "подтвердите, что вы не робот",
    "checkpoint-captcha",
    'data-marker="captcha"',
    "hcaptcha.com",
    "captcha-container",
)

_API_URL_MARKERS = (
    "/web/1/js/items",
    "/web/1/main/items",
    "/api/9/items",
)

_ITEM_SELECTORS = (
    '[data-marker="item"]',
    '[data-marker="catalog-serp"] [data-marker="item"]',
    "article[data-item-id]",
)

_STEALTH_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
window.chrome = { runtime: {} };
Object.defineProperty(navigator, 'languages', { get: () => ['ru-RU', 'ru', 'en-US', 'en'] });
Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
"""


def _is_browser_page_blocked(html: str) -> bool:
    lowered = html.lower()
    return any(marker in lowered for marker in _BLOCK_MARKERS)


class BrowserScraper:
    """Сканирование через браузер — нужно, когда Авито грузит объявления через JavaScript."""

    def __init__(self, config: AppConfig, search_url_builder: Callable[[int], str]) -> None:
        self.config = config
        self.search_url_builder = search_url_builder

    def scan(self) -> tuple[list[Listing], int, int]:
        try:
            from playwright.sync_api import sync_playwright  # noqa: F401
        except ImportError as exc:
            raise RuntimeError(
                "Для режима браузера установите Playwright:\n"
                "  pip install playwright\n"
                "  python -m playwright install chromium"
            ) from exc

        if self.config.browser_headless:
            listings, total_found, pages_scanned, blocked = self._run(headless=True)
            if listings:
                return listings, total_found, pages_scanned
            logger.info(
                "Скрытый браузер не нашёл объявления — пробую видимое окно "
                "(можно пройти капчу вручную)..."
            )
            visible_listings, visible_total, visible_pages, visible_blocked = self._run(
                headless=False
            )
            if visible_listings:
                return visible_listings, visible_total, visible_pages
            blocked = blocked or visible_blocked
            listings = visible_listings
            total_found = visible_total
            pages_scanned = visible_pages
        else:
            listings, total_found, pages_scanned, blocked = self._run(headless=False)

        if blocked and not listings:
            logger.error(
                "Авито показал капчу или заблокировал IP. "
                "Запустите: python -m avito_monitor scan --visible-browser --no-email"
            )

        return listings, total_found or len(listings), pages_scanned

    def _run(self, headless: bool) -> tuple[list[Listing], int, int, bool]:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright

        listings: list[Listing] = []
        seen_ids: set[int] = set()
        avito_total = 0
        pages_scanned = 0
        max_pages = self.config.max_pages
        blocked = False
        consecutive_no_new = 0
        profile_dir = resolve_project_path(self.config.browser_profile_dir)
        profile_dir.mkdir(parents=True, exist_ok=True)

        with sync_playwright() as playwright:
            launch_kwargs: dict[str, Any] = {
                "headless": headless,
                "locale": "ru-RU",
                "viewport": {"width": 1366, "height": 900},
                "user_agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
                ),
                "args": ["--disable-blink-features=AutomationControlled"],
            }
            if self.config.proxy:
                launch_kwargs["proxy"] = {"server": self.config.proxy}

            context = playwright.chromium.launch_persistent_context(
                str(profile_dir),
                **launch_kwargs,
            )
            context.add_init_script(_STEALTH_SCRIPT)
            page = context.pages[0] if context.pages else context.new_page()
            if not headless:
                logger.info(
                    "Открыто окно Chrome. Если появится капча — пройдите её, "
                    "затем программа сама начнёт сканирование и листание страниц."
                )
                try:
                    page.bring_to_front()
                except Exception:
                    pass
            api_payloads: list[dict[str, Any]] = []

            def on_response(response) -> None:
                url = response.url
                if not any(marker in url for marker in _API_URL_MARKERS):
                    return
                try:
                    if response.status != 200:
                        return
                    body = response.json()
                except Exception:
                    return
                if isinstance(body, dict) and body.get("too-many-requests"):
                    logger.warning("Браузер: API заблокирован (429)")
                    return
                if isinstance(body, dict) and body.get("items"):
                    api_payloads.append(body)

            page.on("response", on_response)
            warmed_up = False

            for page_num in range(1, max_pages + 1):
                api_payloads.clear()
                url = self.search_url_builder(page_num)
                mode = "скрытый" if headless else "видимый"
                logger.info("Браузер (%s): страница %s — %s", mode, page_num, url)

                html = self._load_page(page, url, warmed_up=warmed_up)
                warmed_up = True

                if not headless and page_num == 1 and self._item_count(page) == 0:
                    self._wait_for_listings(page, headless=False)
                    html = page.content()

                if _is_browser_page_blocked(html):
                    blocked = True
                    if not headless and page_num == 1:
                        logger.warning(
                            "Браузер: обнаружена капча или блокировка — "
                            "пройдите проверку в окне Chrome"
                        )
                        if self._wait_for_listings(page, headless=False):
                            html = page.content()
                            blocked = False
                        else:
                            self._save_debug(page, html, page_num, "blocked")
                            break
                    else:
                        self._save_debug(page, html, page_num, "blocked")
                        logger.error("Браузер: страница заблокирована (капча / 429)")
                        break

                if page_num == 1:
                    avito_total = _extract_total_count_from_html(html) or 0
                    if avito_total:
                        logger.info("Браузер: на Авито найдено объявлений: %s", avito_total)

                for payload in api_payloads:
                    api_total = _extract_total_count_from_api_payload(payload)
                    if api_total:
                        avito_total = max(avito_total, api_total)

                page_listings, card_count, added, skipped_seen, skipped_filter = (
                    self._ingest_page_listings(page, html, api_payloads, listings, seen_ids)
                )

                if added == 0 and page_num == 1 and card_count == 0:
                    self._save_debug(page, html, page_num, "empty")
                    if not headless and self._wait_for_listings(page, headless=False):
                        html = page.content()
                        blocked = False
                        page_listings, card_count, added, skipped_seen, skipped_filter = (
                            self._ingest_page_listings(
                                page, html, api_payloads, listings, seen_ids
                            )
                        )
                    else:
                        logger.warning(
                            "Браузер: на странице 1 нет объявлений "
                            "(карточек в DOM: %s, ответов API: %s)",
                            card_count,
                            len(api_payloads),
                        )
                        if page_num == 1 and card_count == 0:
                            blocked = True
                            break

                if card_count == 0 and not page_listings:
                    if page_num == 1:
                        blocked = blocked or headless
                        break
                    logger.info("Браузер: страница %s пуста — конец выдачи", page_num)
                    break

                if added == 0:
                    consecutive_no_new += 1
                    logger.info(
                        "Браузер: страница %s — новых объявлений нет "
                        "(карточек %s, дубликатов %s, отфильтровано %s, подряд: %s)",
                        page_num,
                        len(page_listings),
                        skipped_seen,
                        skipped_filter,
                        consecutive_no_new,
                    )
                    if consecutive_no_new >= self.config.browser_max_empty_pages:
                        logger.info(
                            "Браузер: остановка — %s страниц подряд без новых объявлений",
                            consecutive_no_new,
                        )
                        break
                    time.sleep(self.config.request_delay_seconds)
                    continue

                consecutive_no_new = 0
                pages_scanned += 1
                logger.info(
                    "Браузер страница %s: добавлено %s, всего %s "
                    "(дубликатов %s, отфильтровано %s)",
                    page_num,
                    added,
                    len(listings),
                    skipped_seen,
                    skipped_filter,
                )

                time.sleep(self.config.request_delay_seconds)

            context.close()

        reported_total = max(avito_total, len(listings))
        return listings, reported_total, pages_scanned, blocked

    def _load_page(self, page, url: str, warmed_up: bool) -> str:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

        if not warmed_up:
            try:
                page.goto(AVITO_BASE, wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(2000)
            except PlaywrightTimeoutError:
                logger.debug("Браузер: не удалось открыть главную Авито")

        for attempt in range(2):
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=90000)
                self._wait_for_content(page)
                return page.content()
            except PlaywrightTimeoutError as exc:
                if attempt == 0:
                    logger.warning(
                        "Браузер: таймаут загрузки, повтор через 5 сек..."
                    )
                    page.wait_for_timeout(5000)
                    continue
                logger.warning("Браузер: таймаут загрузки страницы: %s", exc)
                return page.content()
        return page.content()

    def _item_count(self, page) -> int:
        for selector in _ITEM_SELECTORS:
            count = page.locator(selector).count()
            if count:
                return count
        return 0

    def _wait_for_listings(self, page, headless: bool) -> bool:
        if headless:
            return False

        timeout_sec = self.config.browser_captcha_wait_seconds
        logger.info(
            "Браузер: ожидание загрузки объявлений до %s сек. "
            "Если видите капчу — пройдите её в окне Chrome.",
            timeout_sec,
        )
        deadline = time.time() + timeout_sec
        while time.time() < deadline:
            if self._item_count(page) > 0:
                logger.info("Браузер: объявления загрузились, продолжаю сканирование")
                self._wait_for_content(page)
                return True
            remaining = int(deadline - time.time())
            if remaining > 0 and remaining % 30 == 0:
                logger.info("Браузер: жду... осталось ~%s сек", remaining)
            page.wait_for_timeout(3000)
            self._scroll_page(page)

        return self._item_count(page) > 0

    def _ingest_page_listings(
        self,
        page,
        html: str,
        api_payloads: list[dict[str, Any]],
        listings: list[Listing],
        seen_ids: set[int],
    ) -> tuple[list[Listing], int, int, int, int]:
        page_listings = self._collect_page_listings(page, html, api_payloads)
        card_count = self._item_count(page)
        added = 0
        skipped_seen = 0
        skipped_filter = 0
        for listing in page_listings:
            if listing.avito_id in seen_ids:
                skipped_seen += 1
                continue
            if not listing.title or not _matches_product(listing.title, self.config.product):
                skipped_filter += 1
                continue
            seen_ids.add(listing.avito_id)
            listings.append(listing)
            added += 1
        return page_listings, card_count, added, skipped_seen, skipped_filter

    def _scroll_page(self, page) -> None:
        page.evaluate("window.scrollBy(0, Math.max(500, window.innerHeight * 0.8))")
        page.wait_for_timeout(800)

    def _wait_for_content(self, page) -> None:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

        try:
            page.wait_for_load_state("networkidle", timeout=30000)
        except PlaywrightTimeoutError:
            pass
        for selector in _ITEM_SELECTORS:
            try:
                page.wait_for_selector(selector, timeout=15000)
                break
            except PlaywrightTimeoutError:
                continue
        for _ in range(4):
            self._scroll_page(page)

    def _collect_page_listings(
        self,
        page,
        html: str,
        api_payloads: list[dict[str, Any]],
    ) -> list[Listing]:
        listings: list[Listing] = []
        seen: set[int] = set()

        for payload in api_payloads:
            for item in payload.get("items") or []:
                listing = _item_from_api_payload(item, self.config)
                if listing and listing.avito_id not in seen:
                    seen.add(listing.avito_id)
                    listings.append(listing)

        if listings:
            logger.info("Браузер: получено %s объявлений из API-ответов", len(listings))
            return listings

        cards = page.locator('[data-marker="item"]')
        card_count = cards.count()
        for index in range(card_count):
            card = cards.nth(index)
            try:
                listing = self._parse_card(card)
            except Exception as exc:
                logger.debug("Браузер: пропуск карточки %s: %s", index, exc)
                continue
            if listing and listing.avito_id not in seen:
                seen.add(listing.avito_id)
                listings.append(listing)

        if listings:
            logger.info("Браузер: получено %s объявлений из DOM", len(listings))
            return listings

        for listing in _parse_page_listings(html, self.config):
            if listing.avito_id not in seen:
                seen.add(listing.avito_id)
                listings.append(listing)
        if listings:
            logger.info("Браузер: получено %s объявлений из HTML/JSON", len(listings))
        return listings

    def _save_debug(self, page, html: str, page_num: int, reason: str) -> None:
        debug_dir = resolve_project_path(self.config.reports_dir) / "debug"
        debug_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        html_path = debug_dir / f"browser_{reason}_p{page_num}_{stamp}.html"
        png_path = debug_dir / f"browser_{reason}_p{page_num}_{stamp}.png"
        try:
            html_path.write_text(html, encoding="utf-8")
            page.screenshot(path=str(png_path), full_page=True)
            logger.info("Диагностика сохранена: %s и %s", html_path, png_path)
        except Exception as exc:
            logger.debug("Не удалось сохранить диагностику: %s", exc)

    def _parse_card(self, card) -> Listing | None:
        link = card.locator('[data-marker="item-title"]').first
        if link.count() == 0:
            link = card.locator("a[itemprop='url']").first
        if link.count() == 0:
            link = card.locator("a[href]").first
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
