from __future__ import annotations

import base64
from datetime import datetime
from pathlib import Path
from typing import Any

from jinja2 import Template

from avito_monitor.models import MarketStats, ScanResult

REPORT_TEMPLATE = Template(
    """
<!DOCTYPE html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <title>Отчёт по рынку {{ product }} — {{ region }}</title>
  <style>
    body { font-family: Arial, sans-serif; color: #1f2937; margin: 0; padding: 24px; background: #f8fafc; }
    .container { max-width: 980px; margin: 0 auto; background: #fff; padding: 28px; border-radius: 12px; }
    h1, h2 { color: #0f172a; }
    .meta { color: #64748b; margin-bottom: 24px; }
    .cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px; margin: 20px 0; }
    .card { background: #eff6ff; border-radius: 10px; padding: 16px; }
    .card strong { display: block; font-size: 24px; margin-top: 6px; }
    table { width: 100%; border-collapse: collapse; margin: 16px 0; }
    th, td { border: 1px solid #e2e8f0; padding: 10px; text-align: left; }
    th { background: #f1f5f9; }
    img { max-width: 100%; border-radius: 8px; margin: 16px 0; border: 1px solid #e2e8f0; }
    a { color: #2563eb; text-decoration: none; }
    .note { background: #fff7ed; border-left: 4px solid #f59e0b; padding: 12px 16px; margin: 20px 0; }
  </style>
</head>
<body>
  <div class="container">
    <h1>Аналитический отчёт: {{ product }}</h1>
    <p class="meta">
      Регион: <strong>{{ region }}</strong><br>
      Дата сканирования: <strong>{{ scanned_at }}</strong><br>
      Источник данных: <strong>{{ source }}</strong><br>
      Всего найдено на Авито: <strong>{{ total_found }}</strong>,
      собрано в отчёт: <strong>{{ total_listings }}</strong>
    </p>

    {% if demo_notice %}
    <div class="note">{{ demo_notice }}</div>
    {% endif %}

    <h2>Ключевые показатели</h2>
    <div class="cards">
      <div class="card">Минимальная цена<strong>{{ min_price }}</strong></div>
      <div class="card">Максимальная цена<strong>{{ max_price }}</strong></div>
      <div class="card">Средняя цена<strong>{{ mean_price }}</strong></div>
      <div class="card">Медианная цена<strong>{{ median_price }}</strong></div>
      <div class="card">Объявлений с ценой<strong>{{ listings_with_price }}</strong></div>
    </div>

    <h2>Перцентили цен</h2>
    <table>
      <tr><th>Перцентиль</th><th>Цена, ₽</th></tr>
      {% for label, value in percentiles.items() %}
      <tr><td>{{ label }}</td><td>{{ format_price(value) }}</td></tr>
      {% endfor %}
    </table>

    <h2>Топ-5 самых дешёвых предложений</h2>
    <table>
      <tr><th>Название</th><th>Город</th><th>Цена</th><th>Ссылка</th></tr>
      {% for item in top_cheapest %}
      <tr>
        <td>{{ item.title }}</td>
        <td>{{ item.city }}</td>
        <td>{{ format_price(item.price) }}</td>
        <td><a href="{{ item.url }}">Открыть</a></td>
      </tr>
      {% endfor %}
    </table>

    <h2>Топ-5 самых дорогих предложений</h2>
    <table>
      <tr><th>Название</th><th>Город</th><th>Цена</th><th>Ссылка</th></tr>
      {% for item in top_expensive %}
      <tr>
        <td>{{ item.title }}</td>
        <td>{{ item.city }}</td>
        <td>{{ format_price(item.price) }}</td>
        <td><a href="{{ item.url }}">Открыть</a></td>
      </tr>
      {% endfor %}
    </table>

  {% for title, chart in charts %}
    <h2>{{ title }}</h2>
    <img src="cid:{{ chart.cid }}" alt="{{ title }}">
  {% endfor %}

    <h2>Выводы</h2>
    <ul>
      {% for insight in insights %}
      <li>{{ insight }}</li>
      {% endfor %}
    </ul>
  </div>
</body>
</html>
"""
)


def _format_price(value: float | int | None) -> str:
    if value is None:
        return "—"
    return f"{int(value):,}".replace(",", " ") + " ₽"


def _build_insights(stats: MarketStats, scan: ScanResult) -> list[str]:
    insights: list[str] = []
    if stats.mean_price and stats.median_price:
        if stats.mean_price > stats.median_price * 1.15:
            insights.append(
                "Средняя цена заметно выше медианы — на рынке есть дорогие выбросы."
            )
        elif stats.mean_price < stats.median_price * 0.9:
            insights.append(
                "Средняя цена ниже медианы — преобладают более доступные предложения."
            )
        else:
            insights.append("Распределение цен относительно сбалансировано.")

    if stats.listings_by_city:
        top_city = max(stats.listings_by_city, key=stats.listings_by_city.get)
        insights.append(
            f"Наибольшее предложение сосредоточено в городе {top_city} "
            f"({stats.listings_by_city[top_city]} объявлений)."
        )

    if stats.avg_price_per_city:
        cheapest_city = min(stats.avg_price_per_city, key=stats.avg_price_per_city.get)
        expensive_city = max(stats.avg_price_per_city, key=stats.avg_price_per_city.get)
        insights.append(
            f"Самые низкие средние цены в {cheapest_city}, "
            f"самые высокие — в {expensive_city}."
        )

    if len(stats.historical_trend) >= 2:
        first = stats.historical_trend[0]
        last = stats.historical_trend[-1]
        if first.get("avg_price") and last.get("avg_price"):
            delta = float(last["avg_price"]) - float(first["avg_price"])
            direction = "выросла" if delta > 0 else "снизилась"
            insights.append(
                f"За период наблюдений средняя цена {direction} "
                f"на {abs(int(delta)):,}".replace(",", " ") + " ₽."
            )

    insights.append(
        f"В текущем сканировании обработано {scan.pages_scanned} страниц результатов Авито."
    )
    return insights


CHART_TITLES = {
    "price_distribution": "Распределение цен",
    "price_by_city": "Средняя цена по городам",
    "listings_by_city": "Количество объявлений по городам",
    "price_boxplot": "Разброс цен по городам",
    "historical_trend": "Динамика рынка",
}


class ReportBuilder:
    def build_html(
        self,
        scan: ScanResult,
        stats: MarketStats,
        chart_paths: dict[str, Path],
    ) -> tuple[str, list[dict[str, Any]]]:
        demo_notice = ""
        if scan.source.startswith("demo"):
            demo_notice = (
                "Данные получены в демо-режиме или через fallback из-за ограничений доступа к Авито. "
                "Для реального мониторинга запускайте программу с российского IP или через прокси (PROXY)."
            )

        chart_blocks = [
            {"title": CHART_TITLES.get(key, key), "cid": key, "path": path}
            for key, path in chart_paths.items()
        ]

        html = REPORT_TEMPLATE.render(
            product=scan.product,
            region=scan.region,
            scanned_at=scan.scanned_at.strftime("%d.%m.%Y %H:%M UTC"),
            source=scan.source,
            total_found=scan.total_found,
            total_listings=stats.total_listings,
            min_price=_format_price(stats.min_price),
            max_price=_format_price(stats.max_price),
            mean_price=_format_price(int(stats.mean_price) if stats.mean_price else None),
            median_price=_format_price(int(stats.median_price) if stats.median_price else None),
            listings_with_price=stats.listings_with_price,
            percentiles=stats.price_percentiles,
            top_cheapest=stats.top_cheapest,
            top_expensive=stats.top_expensive,
            charts=[{"title": c["title"], "cid": c["cid"]} for c in chart_blocks],
            insights=_build_insights(stats, scan),
            demo_notice=demo_notice,
            format_price=_format_price,
        )
        return html, chart_blocks

    def save_report(
        self,
        reports_dir: Path,
        scan: ScanResult,
        html: str,
        chart_paths: dict[str, Path],
    ) -> Path:
        timestamp = scan.scanned_at.strftime("%Y%m%d_%H%M%S")
        report_dir = reports_dir / f"report_{timestamp}"
        report_dir.mkdir(parents=True, exist_ok=True)
        html_path = report_dir / "report.html"
        html_path.write_text(html, encoding="utf-8")

        for key, source in chart_paths.items():
            target = report_dir / source.name
            target.write_bytes(source.read_bytes())

        return html_path

    @staticmethod
    def embed_charts_for_email(chart_blocks: list[dict[str, Any]]) -> list[dict[str, str]]:
        embedded = []
        for block in chart_blocks:
            content = Path(block["path"]).read_bytes()
            embedded.append(
                {
                    "cid": block["cid"],
                    "filename": Path(block["path"]).name,
                    "content": base64.b64encode(content).decode("ascii"),
                }
            )
        return embedded
