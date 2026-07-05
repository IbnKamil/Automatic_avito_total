from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml
from dotenv import load_dotenv


@dataclass
class AppConfig:
    product: str = "Скамейки"
    region: str = "Дагестан"
    region_slug: str = "dagestan"
    location_id: int = 646710
    report_interval_days: int = 3
    max_pages: int = 50
    request_delay_seconds: float = 2.0
    database_path: str = "data/listings.db"
    reports_dir: str = "reports"
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    smtp_use_tls: bool = True
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    smtp_to: str = ""
    proxy: str = ""
    demo_mode: bool = False


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    return int(value) if value else default


def _env_float(name: str, default: float) -> float:
    value = os.getenv(name)
    return float(value) if value else default


def load_config(config_path: str | Path = "config.yaml") -> AppConfig:
    load_dotenv()
    config = AppConfig()
    path = Path(config_path)
    if path.exists():
        with path.open(encoding="utf-8") as handle:
            data = yaml.safe_load(handle) or {}
        config.product = data.get("product", config.product)
        config.region = data.get("region", config.region)
        config.region_slug = data.get("region_slug", config.region_slug)
        config.location_id = int(data.get("location_id", config.location_id))
        config.report_interval_days = int(
            data.get("report_interval_days", config.report_interval_days)
        )
        config.max_pages = int(data.get("max_pages", config.max_pages))
        config.request_delay_seconds = float(
            data.get("request_delay_seconds", config.request_delay_seconds)
        )
        config.database_path = data.get("database_path", config.database_path)
        config.reports_dir = data.get("reports_dir", config.reports_dir)
        smtp = data.get("smtp", {})
        config.smtp_host = smtp.get("host", config.smtp_host)
        config.smtp_port = int(smtp.get("port", config.smtp_port))
        config.smtp_use_tls = bool(smtp.get("use_tls", config.smtp_use_tls))
        config.smtp_username = smtp.get("username", config.smtp_username)
        config.smtp_password = smtp.get("password", config.smtp_password)
        config.smtp_from = smtp.get("from_email", config.smtp_from)
        config.smtp_to = smtp.get("to_email", config.smtp_to)
        config.proxy = data.get("proxy", config.proxy)

    config.product = os.getenv("PRODUCT", config.product)
    config.region = os.getenv("REGION", config.region)
    config.region_slug = os.getenv("REGION_SLUG", config.region_slug)
    config.location_id = _env_int("LOCATION_ID", config.location_id)
    config.report_interval_days = _env_int(
        "REPORT_INTERVAL_DAYS", config.report_interval_days
    )
    config.max_pages = _env_int("MAX_PAGES", config.max_pages)
    config.request_delay_seconds = _env_float(
        "REQUEST_DELAY_SECONDS", config.request_delay_seconds
    )
    config.database_path = os.getenv("DATABASE_PATH", config.database_path)
    config.reports_dir = os.getenv("REPORTS_DIR", config.reports_dir)
    config.smtp_host = os.getenv("SMTP_HOST", config.smtp_host)
    config.smtp_port = _env_int("SMTP_PORT", config.smtp_port)
    config.smtp_use_tls = _env_bool("SMTP_USE_TLS", config.smtp_use_tls)
    config.smtp_username = os.getenv("SMTP_USERNAME", config.smtp_username)
    config.smtp_password = os.getenv("SMTP_PASSWORD", config.smtp_password)
    config.smtp_from = os.getenv("SMTP_FROM", config.smtp_from or config.smtp_username)
    config.smtp_to = os.getenv("SMTP_TO", config.smtp_to)
    config.proxy = os.getenv("PROXY", config.proxy)
    config.demo_mode = _env_bool("DEMO_MODE", config.demo_mode)
    return config
