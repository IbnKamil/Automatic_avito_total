from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

from avito_monitor.models import Listing, ScanResult


class ListingStorage:
    def __init__(self, database_path: str) -> None:
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        return connection

    def _init_db(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS scans (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    scanned_at TEXT NOT NULL,
                    product TEXT NOT NULL,
                    region TEXT NOT NULL,
                    total_found INTEGER NOT NULL,
                    pages_scanned INTEGER NOT NULL,
                    source TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS listings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    scan_id INTEGER NOT NULL,
                    avito_id INTEGER NOT NULL,
                    title TEXT NOT NULL,
                    price INTEGER,
                    price_string TEXT,
                    url TEXT NOT NULL,
                    location TEXT,
                    city TEXT,
                    image_url TEXT,
                    posted_at TEXT,
                    raw_json TEXT,
                    FOREIGN KEY (scan_id) REFERENCES scans(id)
                );

                CREATE INDEX IF NOT EXISTS idx_listings_scan_id ON listings(scan_id);
                CREATE INDEX IF NOT EXISTS idx_listings_avito_id ON listings(avito_id);
                """
            )

    def save_scan(self, result: ScanResult) -> int:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO scans (scanned_at, product, region, total_found, pages_scanned, source)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    result.scanned_at.isoformat(),
                    result.product,
                    result.region,
                    result.total_found,
                    result.pages_scanned,
                    result.source,
                ),
            )
            scan_id = int(cursor.lastrowid)
            connection.executemany(
                """
                INSERT INTO listings (
                    scan_id, avito_id, title, price, price_string, url,
                    location, city, image_url, posted_at, raw_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        scan_id,
                        listing.avito_id,
                        listing.title,
                        listing.price,
                        listing.price_string,
                        listing.url,
                        listing.location,
                        listing.city,
                        listing.image_url,
                        listing.posted_at,
                        json.dumps(listing.raw, ensure_ascii=False) if listing.raw else None,
                    )
                    for listing in result.listings
                ],
            )
            result.scan_id = scan_id
            return scan_id

    def get_latest_scan(self, product: str, region: str) -> ScanResult | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM scans
                WHERE product = ? AND region = ?
                ORDER BY scanned_at DESC
                LIMIT 1
                """,
                (product, region),
            ).fetchone()
            if not row:
                return None
            listings = self._fetch_listings_for_scan(connection, int(row["id"]))
            return ScanResult(
                scan_id=int(row["id"]),
                scanned_at=datetime.fromisoformat(row["scanned_at"]),
                product=row["product"],
                region=row["region"],
                listings=listings,
                total_found=int(row["total_found"]),
                pages_scanned=int(row["pages_scanned"]),
                source=row["source"],
            )

    def get_scan_history(self, product: str, region: str, limit: int = 20) -> list[dict]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT s.id, s.scanned_at, s.total_found, s.source,
                       COUNT(l.id) AS collected,
                       AVG(l.price) AS avg_price,
                       MIN(l.price) AS min_price,
                       MAX(l.price) AS max_price
                FROM scans s
                LEFT JOIN listings l ON l.scan_id = s.id AND l.price IS NOT NULL
                WHERE s.product = ? AND s.region = ?
                GROUP BY s.id
                ORDER BY s.scanned_at ASC
                LIMIT ?
                """,
                (product, region, limit),
            ).fetchall()
            return [dict(row) for row in rows]

    def _fetch_listings_for_scan(
        self, connection: sqlite3.Connection, scan_id: int
    ) -> list[Listing]:
        rows = connection.execute(
            "SELECT * FROM listings WHERE scan_id = ? ORDER BY price ASC NULLS LAST",
            (scan_id,),
        ).fetchall()
        listings: list[Listing] = []
        for row in rows:
            raw = json.loads(row["raw_json"]) if row["raw_json"] else {}
            listings.append(
                Listing(
                    avito_id=int(row["avito_id"]),
                    title=row["title"],
                    price=row["price"],
                    price_string=row["price_string"] or "",
                    url=row["url"],
                    location=row["location"] or "",
                    city=row["city"] or "",
                    image_url=row["image_url"] or "",
                    posted_at=row["posted_at"],
                    raw=raw,
                )
            )
        return listings
