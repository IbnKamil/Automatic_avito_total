from __future__ import annotations

import argparse
import logging
import sys

from avito_monitor.config import load_config
from avito_monitor.scraper import resolve_location_id
from avito_monitor.service import MonitorService


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Мониторинг объявлений Авито с аналитикой и email-отчётами",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    scan_parser = subparsers.add_parser("scan", help="Выполнить одно сканирование и отчёт")
    scan_parser.add_argument("--no-email", action="store_true", help="Не отправлять email")
    scan_parser.add_argument("--demo", action="store_true", help="Использовать демо-данные")

    schedule_parser = subparsers.add_parser(
        "schedule",
        help="Запустить планировщик (отчёт каждые N дней)",
    )
    schedule_parser.add_argument("--demo", action="store_true", help="Использовать демо-данные")

    subparsers.add_parser("resolve-region", help="Определить slug и location_id региона на Авито")

    config_parser = subparsers.add_parser("show-config", help="Показать текущую конфигурацию")
    config_parser.add_argument("--product", help="Товар для поиска")
    config_parser.add_argument("--region", help="Регион поиска")

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
        return 0

    service = MonitorService(config)

    if args.command == "scan":
        service.run_scan_and_report(send_email=not args.no_email)
        return 0

    if args.command == "schedule":
        service.start_scheduler()
        return 0

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
