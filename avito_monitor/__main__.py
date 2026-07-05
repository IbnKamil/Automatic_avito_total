from __future__ import annotations

import argparse
import logging
import sys

from pathlib import Path

from avito_monitor.config import load_config
from avito_monitor.scraper import AvitoScraperError, resolve_location_id
from avito_monitor.mailer import EmailSender
from avito_monitor.service import MonitorService


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Мониторинг объявлений Авито с аналитикой и email-отчётами",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    scan_parser = subparsers.add_parser("scan", help="Выполнить одно сканирование и отчёт")
    scan_parser.add_argument("--no-email", action="store_true", help="Не отправлять email")
    scan_parser.add_argument("--demo", action="store_true", help="Использовать демо-данные")
    scan_parser.add_argument(
        "--browser-only",
        action="store_true",
        help="Только браузер Playwright, без HTTP/API (быстрее при блокировке 429)",
    )
    scan_parser.add_argument(
        "--visible-browser",
        action="store_true",
        help="Открыть видимое окно браузера (включено по умолчанию)",
    )
    scan_parser.add_argument(
        "--headless",
        action="store_true",
        help="Скрытый браузер без окна (может не пройти капчу Авито)",
    )
    scan_parser.add_argument(
        "--no-pdf",
        action="store_true",
        help="Не создавать PDF-версию отчёта",
    )

    schedule_parser = subparsers.add_parser(
        "schedule",
        help="Запустить планировщик (отчёт каждые N дней)",
    )
    schedule_parser.add_argument("--demo", action="store_true", help="Использовать демо-данные")
    schedule_parser.add_argument(
        "--browser-only",
        action="store_true",
        help="Только браузер Playwright, без HTTP/API",
    )

    subparsers.add_parser("resolve-region", help="Определить slug и location_id региона на Авито")

    config_parser = subparsers.add_parser("show-config", help="Показать текущую конфигурацию")
    config_parser.add_argument("--product", help="Товар для поиска")
    config_parser.add_argument("--region", help="Регион поиска")

    subparsers.add_parser("test-email", help="Проверить настройки SMTP тестовым письмом")

    return parser


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )


def main(argv: list[str] | None = None) -> int:
    configure_logging()
    parser = build_parser()
    args = parser.parse_args(argv)
    config = load_config()

    if getattr(args, "demo", False):
        config.demo_mode = True
    if getattr(args, "browser_only", False):
        config.browser_only = True
        config.use_browser = True
    if getattr(args, "visible_browser", False):
        config.browser_headless = False
        config.use_browser = True
    if getattr(args, "headless", False):
        config.browser_headless = True
        config.use_browser = True
    if getattr(args, "no_pdf", False):
        config.export_pdf = False

    if args.command == "resolve-region":
        slug, location_id = resolve_location_id(config.region)
        print(f"Регион: {config.region}")
        print(f"Slug: {slug}")
        print(f"Location ID: {location_id}")
        return 0

    if args.command == "show-config":
        if args.product:
            config.product = args.product
        if args.region:
            config.region = args.region
        print(f"Товар: {config.product}")
        print(f"Регион: {config.region}")
        print(f"Slug: {config.region_slug}")
        print(f"Location ID: {config.location_id}")
        print(f"Интервал отчётов: {config.report_interval_days} дн.")
        print(f"Email: {config.smtp_to or 'не настроен'}")
        print(f"Демо-режим: {config.demo_mode}")
        print(f"Режим браузера: {config.use_browser}")
        print(f"Только браузер: {config.browser_only}")
        print(f"Скрытый браузер: {config.browser_headless}")
        print(f"Экспорт в PDF: {config.export_pdf}")
        print(f"Папка отчётов: {Path(config.reports_dir).resolve()}")
        print(f"База данных: {Path(config.database_path).resolve()}")
        return 0

    if args.command == "test-email":
        mailer = EmailSender(config)
        mailer.send_test_email()
        print(f"Тестовое письмо отправлено на {config.smtp_to}")
        return 0

    service = MonitorService(config)

    if args.command == "scan":
        try:
            report_path = service.run_scan_and_report(send_email=not args.no_email)
        except AvitoScraperError as exc:
            print(f"\nОшибка сканирования: {exc}\n")
            return 1
        print(f"\nГотово. Отчёт сохранён в:\n{report_path.resolve()}\n")
        return 0

    if args.command == "schedule":
        service.start_scheduler()
        return 0

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
