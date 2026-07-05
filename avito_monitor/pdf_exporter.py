from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)


class PdfExportError(RuntimeError):
    pass


def export_html_to_pdf(html_path: Path, pdf_path: Path | None = None) -> Path:
    html_path = html_path.resolve()
    if not html_path.exists():
        raise PdfExportError(f"HTML-отчёт не найден: {html_path}")

    target = pdf_path or html_path.with_name("report.pdf")

    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise PdfExportError(
            "Для экспорта в PDF установите Playwright:\n"
            "  pip install playwright\n"
            "  python -m playwright install chromium"
        ) from exc

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        try:
            page.goto(html_path.as_uri(), wait_until="networkidle", timeout=120000)
        except PlaywrightTimeoutError:
            page.goto(html_path.as_uri(), wait_until="domcontentloaded", timeout=120000)
        page.wait_for_timeout(1500)
        page.pdf(
            path=str(target),
            format="A4",
            print_background=True,
            margin={"top": "12mm", "right": "10mm", "bottom": "12mm", "left": "10mm"},
        )
        browser.close()

    logger.info("PDF-отчёт сохранён: %s", target.resolve())
    return target
