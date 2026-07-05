from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.interval import IntervalTrigger

from avito_monitor.analyzer import MarketAnalyzer
from avito_monitor.config import AppConfig
from avito_monitor.mailer import EmailSender
from avito_monitor.reporter import ReportBuilder
from avito_monitor.scraper import AvitoScraper
from avito_monitor.storage import ListingStorage

logger = logging.getLogger(__name__)


class MonitorService:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.storage = ListingStorage(config.database_path)
        self.scraper = AvitoScraper(config)
        self.analyzer = MarketAnalyzer(self.storage)
        self.reporter = ReportBuilder()
        self.mailer = EmailSender(config)
        self.reports_dir = Path(config.reports_dir)

    def run_scan_and_report(self, send_email: bool = True) -> Path:
        logger.info(
            "Запуск сканирования: товар='%s', регион='%s'",
            self.config.product,
            self.config.region,
        )
        scan = self.scraper.scan()
        self.storage.save_scan(scan)
        stats = self.analyzer.analyze(scan)

        chart_dir = self.reports_dir / "charts" / scan.scanned_at.strftime("%Y%m%d_%H%M%S")
        chart_paths = self.analyzer.build_charts(scan, stats, chart_dir)
        html, chart_blocks = self.reporter.build_html(scan, stats, chart_paths)
        report_path = self.reporter.save_report(self.reports_dir, scan, html, chart_paths)

        if send_email:
            if self.mailer.is_configured():
                subject = (
                    f"Отчёт по рынку {self.config.product} — "
                    f"{self.config.region} ({datetime.utcnow().strftime('%d.%m.%Y')})"
                )
                embedded = ReportBuilder.embed_charts_for_email(chart_blocks)
                self.mailer.send_report(subject, html, embedded, report_path)
                logger.info("Отчёт отправлен на %s", self.config.smtp_to)
            else:
                logger.warning(
                    "Email не настроен. Отчёт сохранён локально: %s", report_path
                )

        logger.info("Сканирование завершено. Отчёт: %s", report_path)
        return report_path

    def start_scheduler(self) -> None:
        scheduler = BlockingScheduler()
        scheduler.add_job(
            lambda: self.run_scan_and_report(send_email=True),
            trigger=IntervalTrigger(days=self.config.report_interval_days),
            id="avito_report",
            replace_existing=True,
            next_run_time=datetime.utcnow(),
        )
        logger.info(
            "Планировщик запущен. Интервал отчётов: каждые %s дн.",
            self.config.report_interval_days,
        )
        try:
            scheduler.start()
        except (KeyboardInterrupt, SystemExit):
            logger.info("Планировщик остановлен")
