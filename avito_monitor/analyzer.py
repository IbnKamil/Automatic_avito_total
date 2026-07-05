from __future__ import annotations

from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import seaborn as sns

from avito_monitor.models import Listing, MarketStats, ScanResult
from avito_monitor.storage import ListingStorage

sns.set_theme(style="whitegrid", font_scale=1.0)
plt.rcParams["font.family"] = "DejaVu Sans"


class MarketAnalyzer:
    def __init__(self, storage: ListingStorage) -> None:
        self.storage = storage

    def analyze(self, scan: ScanResult) -> MarketStats:
        df = self._listings_to_frame(scan.listings)
        priced = df.dropna(subset=["price"]).copy()
        history = self.storage.get_scan_history(scan.product, scan.region)

        percentiles: dict[str, float] = {}
        if not priced.empty:
            for label, value in zip(
                ["p10", "p25", "p50", "p75", "p90"],
                np.percentile(priced["price"], [10, 25, 50, 75, 90]),
            ):
                percentiles[label] = float(value)

        avg_by_city: dict[str, float] = {}
        count_by_city: dict[str, int] = {}
        if not priced.empty:
            city_stats = priced.groupby("city")["price"].agg(["mean", "count"])
            avg_by_city = {
                city: float(row["mean"]) for city, row in city_stats.iterrows()
            }
            count_by_city = {
                city: int(row["count"]) for city, row in city_stats.iterrows()
            }

        def top_rows(frame: pd.DataFrame, ascending: bool) -> list[dict[str, Any]]:
            ordered = frame.sort_values("price", ascending=ascending).head(5)
            return [
                {
                    "title": row["title"],
                    "price": int(row["price"]),
                    "city": row["city"],
                    "url": row["url"],
                }
                for _, row in ordered.iterrows()
            ]

        return MarketStats(
            total_listings=len(df),
            listings_with_price=len(priced),
            min_price=int(priced["price"].min()) if not priced.empty else None,
            max_price=int(priced["price"].max()) if not priced.empty else None,
            mean_price=float(priced["price"].mean()) if not priced.empty else None,
            median_price=float(priced["price"].median()) if not priced.empty else None,
            std_price=float(priced["price"].std(ddof=0)) if len(priced) > 1 else None,
            avg_price_per_city=avg_by_city,
            listings_by_city=count_by_city,
            price_percentiles=percentiles,
            top_cheapest=top_rows(priced, ascending=True) if not priced.empty else [],
            top_expensive=top_rows(priced, ascending=False) if not priced.empty else [],
            historical_trend=history,
        )

    def build_charts(
        self, scan: ScanResult, stats: MarketStats, output_dir: Path
    ) -> dict[str, Path]:
        output_dir.mkdir(parents=True, exist_ok=True)
        df = self._listings_to_frame(scan.listings)
        priced = df.dropna(subset=["price"]).copy()
        charts: dict[str, Path] = {}

        if not priced.empty:
            charts["price_distribution"] = self._chart_price_distribution(
                priced, output_dir / "price_distribution.png"
            )
            charts["price_by_city"] = self._chart_price_by_city(
                priced, output_dir / "price_by_city.png"
            )
            charts["listings_by_city"] = self._chart_listings_by_city(
                priced, output_dir / "listings_by_city.png"
            )
            charts["price_boxplot"] = self._chart_boxplot(
                priced, output_dir / "price_boxplot.png"
            )

        if stats.historical_trend:
            charts["historical_trend"] = self._chart_historical_trend(
                stats.historical_trend, output_dir / "historical_trend.png"
            )

        return charts

    def _listings_to_frame(self, listings: list[Listing]) -> pd.DataFrame:
        return pd.DataFrame(
            [
                {
                    "avito_id": listing.avito_id,
                    "title": listing.title,
                    "price": listing.price,
                    "city": listing.city or "Не указан",
                    "location": listing.location,
                    "url": listing.url,
                    "posted_at": listing.posted_at,
                }
                for listing in listings
            ]
        )

    def _chart_price_distribution(self, priced: pd.DataFrame, path: Path) -> Path:
        fig, ax = plt.subplots(figsize=(10, 6))
        sns.histplot(priced["price"], bins=20, kde=True, ax=ax, color="#2f6fed")
        ax.set_title("Распределение цен на скамейки")
        ax.set_xlabel("Цена, ₽")
        ax.set_ylabel("Количество объявлений")
        fig.tight_layout()
        fig.savefig(path, dpi=150)
        plt.close(fig)
        return path

    def _chart_price_by_city(self, priced: pd.DataFrame, path: Path) -> Path:
        city_avg = priced.groupby("city")["price"].mean().sort_values(ascending=False).head(10)
        fig, ax = plt.subplots(figsize=(10, 6))
        sns.barplot(
            x=city_avg.values,
            y=city_avg.index,
            hue=city_avg.index,
            palette="Blues_r",
            ax=ax,
            legend=False,
        )
        ax.set_title("Средняя цена по городам (топ-10)")
        ax.set_xlabel("Средняя цена, ₽")
        ax.set_ylabel("Город")
        fig.tight_layout()
        fig.savefig(path, dpi=150)
        plt.close(fig)
        return path

    def _chart_listings_by_city(self, priced: pd.DataFrame, path: Path) -> Path:
        city_count = priced["city"].value_counts().head(10)
        fig, ax = plt.subplots(figsize=(10, 6))
        sns.barplot(
            x=city_count.values,
            y=city_count.index,
            hue=city_count.index,
            palette="Greens_r",
            ax=ax,
            legend=False,
        )
        ax.set_title("Количество объявлений по городам (топ-10)")
        ax.set_xlabel("Количество")
        ax.set_ylabel("Город")
        fig.tight_layout()
        fig.savefig(path, dpi=150)
        plt.close(fig)
        return path

    def _chart_boxplot(self, priced: pd.DataFrame, path: Path) -> Path:
        top_cities = priced["city"].value_counts().head(6).index.tolist()
        subset = priced[priced["city"].isin(top_cities)]
        fig, ax = plt.subplots(figsize=(10, 6))
        sns.boxplot(data=subset, x="city", y="price", ax=ax, color="#8ec5ff")
        ax.set_title("Разброс цен по основным городам")
        ax.set_xlabel("Город")
        ax.set_ylabel("Цена, ₽")
        ax.tick_params(axis="x", rotation=25)
        fig.tight_layout()
        fig.savefig(path, dpi=150)
        plt.close(fig)
        return path

    def _chart_historical_trend(self, history: list[dict[str, Any]], path: Path) -> Path:
        frame = pd.DataFrame(history)
        frame["scanned_at"] = pd.to_datetime(frame["scanned_at"])
        fig, ax1 = plt.subplots(figsize=(10, 6))
        ax1.plot(
            frame["scanned_at"],
            frame["collected"],
            marker="o",
            color="#2f6fed",
            label="Собрано объявлений",
        )
        ax1.set_ylabel("Количество объявлений")
        ax1.set_xlabel("Дата сканирования")
        if frame["avg_price"].notna().any():
            ax2 = ax1.twinx()
            ax2.plot(
                frame["scanned_at"],
                frame["avg_price"],
                marker="s",
                color="#e67e22",
                label="Средняя цена",
            )
            ax2.set_ylabel("Средняя цена, ₽")
        ax1.set_title("Динамика рынка по истории сканирований")
        fig.tight_layout()
        fig.savefig(path, dpi=150)
        plt.close(fig)
        return path
