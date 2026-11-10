from __future__ import annotations

import asyncio
import csv
import io
import logging
import zipfile
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

log = logging.getLogger("AST-Liquidity-Radar.data")

BINANCE_ARCHIVE = "https://data.binance.vision/data/futures/um"
XAUS_API = "https://xaus.com/api/v1/chart"

TF_MS = {
    "1m": 60_000,
    "5m": 300_000,
    "15m": 900_000,
    "30m": 1_800_000,
    "1h": 3_600_000,
    "4h": 14_400_000,
    "1d": 86_400_000,
}


def _previous_months(now: datetime, count: int = 2) -> list[datetime]:
    first = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    cursor = first - timedelta(days=1)
    months = []
    for _ in range(count):
        months.append(cursor.replace(day=1))
        cursor = cursor.replace(day=1) - timedelta(days=1)
    return months


async def _download_archive(client: httpx.AsyncClient, url: str, semaphore: asyncio.Semaphore) -> list[dict[str, Any]]:
    async with semaphore:
        try:
            response = await client.get(url, timeout=35.0)
            if response.status_code == 404:
                return []
            response.raise_for_status()
            with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
                csv_name = next((name for name in archive.namelist() if name.lower().endswith(".csv")), None)
                if not csv_name:
                    return []
                rows = []
                with archive.open(csv_name) as raw:
                    wrapper = io.TextIOWrapper(raw, encoding="utf-8", newline="")
                    for row in csv.reader(wrapper):
                        if len(row) < 7:
                            continue
                        try:
                            opened = int(row[0])
                            closed = int(row[6])
                            candle = {
                                "t": opened,
                                "o": float(row[1]),
                                "h": float(row[2]),
                                "l": float(row[3]),
                                "c": float(row[4]),
                                "v": float(row[5]),
                            }
                            if closed < int(datetime.now(timezone.utc).timestamp() * 1000):
                                rows.append(candle)
                        except (TypeError, ValueError, IndexError):
                            # Skip a possible CSV header or malformed row.
                            continue
                return rows
        except (httpx.HTTPError, zipfile.BadZipFile, OSError) as exc:
            log.warning("Historical archive unavailable (%s): %s", url, exc)
            return []


def resample_from_1m(candles: list[dict[str, Any]], timeframe: str, limit: int) -> list[dict[str, Any]]:
    """Aggregate closed 1m candles into UTC-aligned OHLCV bars."""
    duration = TF_MS[timeframe]
    grouped: dict[int, list[dict[str, Any]]] = {}
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    for candle in candles:
        bucket = (int(candle["t"]) // duration) * duration
        if bucket + duration > now_ms:
            continue
        grouped.setdefault(bucket, []).append(candle)

    result = []
    for bucket, rows in sorted(grouped.items()):
        rows.sort(key=lambda item: int(item["t"]))
        result.append({
            "t": bucket,
            "o": float(rows[0]["o"]),
            "h": max(float(item["h"]) for item in rows),
            "l": min(float(item["l"]) for item in rows),
            "c": float(rows[-1]["c"]),
            "v": sum(float(item.get("v") or 0.0) for item in rows),
        })
    return result[-limit:]


async def load_btc_um_history(
    client: httpx.AsyncClient,
    symbol: str = "BTCUSDT",
    limit: int = 10_000,
) -> dict[str, Any]:
    """Load two completed months plus current-month daily USD-M Futures 1m archives."""
    now = datetime.now(timezone.utc)
    urls = []
    for month in _previous_months(now, 2):
        label = month.strftime("%Y-%m")
        name = f"{symbol}-1m-{label}.zip"
        urls.append(f"{BINANCE_ARCHIVE}/monthly/klines/{symbol}/1m/{name}")

    # Daily archives are published on the following day. Fetch all completed
    # days of this month to bridge the gap between the last monthly archive
    # and the current WebSocket stream.
    day = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    while day.date() < now.date():
        label = day.strftime("%Y-%m-%d")
        name = f"{symbol}-1m-{label}.zip"
        urls.append(f"{BINANCE_ARCHIVE}/daily/klines/{symbol}/1m/{name}")
        day += timedelta(days=1)

    semaphore = asyncio.Semaphore(4)
    pages = await asyncio.gather(*(_download_archive(client, url, semaphore) for url in urls))
    by_time: dict[int, dict[str, Any]] = {}
    for page in pages:
        for candle in page:
            by_time[int(candle["t"])] = candle
    one_minute = [by_time[key] for key in sorted(by_time)]
    if len(one_minute) < 30:
        raise RuntimeError(f"Binance public archive returned only {len(one_minute)} BTC 1m bars")

    data = {"1m": one_minute[-limit:]}
    for tf in ("5m", "15m", "30m", "1h", "4h", "1d"):
        data[tf] = resample_from_1m(one_minute, tf, limit)

    counts = {tf: len(rows) for tf, rows in data.items()}
    log.info("BTC historical archive loaded: %s", counts)
    return {
        "candles": data,
        "source": "Binance public USD-M Futures archive (1m klines; higher TFs aggregated)",
        "bars": counts,
    }


def _normalise_chart_points(payload: dict[str, Any]) -> list[dict[str, Any]]:
    points = payload.get("points")
    if points is None:
        points = payload.get("data")
    result = []
    if not isinstance(points, list):
        return result
    for point in points:
        try:
            stamp = int(point.get("t", point.get("timestamp")))
            if stamp < 10_000_000_000:
                stamp *= 1000
            candle = {
                "t": stamp,
                "o": float(point["o"]),
                "h": float(point["h"]),
                "l": float(point["l"]),
                "c": float(point["c"]),
                "v": float(point.get("v") or 0.0),
            }
            if candle["t"] + 60_000 < int(datetime.now(timezone.utc).timestamp() * 1000):
                result.append(candle)
        except (AttributeError, KeyError, TypeError, ValueError):
            continue
    unique = {int(item["t"]): item for item in result}
    return [unique[key] for key in sorted(unique)]


async def _gold_chart(
    client: httpx.AsyncClient,
    date_range: str,
    interval: str,
) -> list[dict[str, Any]]:
    response = await client.get(
        XAUS_API,
        params={"symbol": "xau", "range": date_range, "interval": interval},
        timeout=30.0,
    )
    response.raise_for_status()
    return _normalise_chart_points(response.json())


async def load_gold_history(client: httpx.AsyncClient, limit: int = 10_000) -> dict[str, Any]:
    """Load XAU reference OHLCV at native Yahoo-proxy intervals; 4h is aggregated."""
    specs = {
        "1m": ("5d", "1m"),
        "5m": ("1mo", "5m"),
        "15m": ("1mo", "15m"),
        "30m": ("1mo", "30m"),
        "1h": ("2y", "1h"),
        "1d": ("5y", "1d"),
    }
    semaphore = asyncio.Semaphore(3)

    async def fetch_one(tf: str, date_range: str, interval: str):
        async with semaphore:
            try:
                rows = await _gold_chart(client, date_range, interval)
                return tf, rows
            except (httpx.HTTPError, ValueError, TypeError) as exc:
                log.warning("Gold history unavailable for %s: %s", tf, exc)
                return tf, []

    fetched = await asyncio.gather(*(fetch_one(tf, rg, iv) for tf, (rg, iv) in specs.items()))
    data = {tf: rows[-limit:] for tf, rows in fetched}
    data["4h"] = resample_from_1m([], "4h", limit)
    one_hour = data.get("1h", [])
    data["4h"] = _resample_from_source(one_hour, "4h", limit)

    counts = {tf: len(rows) for tf, rows in data.items()}
    if sum(counts.values()) == 0:
        raise RuntimeError("Gold reference chart provider returned no OHLCV candles")
    return {
        "candles": data,
        "source": "XAUS XAU chart (Yahoo Finance proxy; reference feed, not Vantage CFD execution data)",
        "bars": counts,
    }


def _resample_from_source(candles: list[dict[str, Any]], timeframe: str, limit: int) -> list[dict[str, Any]]:
    duration = TF_MS[timeframe]
    groups: dict[int, list[dict[str, Any]]] = {}
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    for candle in candles:
        bucket = (int(candle["t"]) // duration) * duration
        if bucket + duration <= now_ms:
            groups.setdefault(bucket, []).append(candle)
    result = []
    for bucket, rows in sorted(groups.items()):
        rows.sort(key=lambda item: int(item["t"]))
        result.append({
            "t": bucket,
            "o": float(rows[0]["o"]),
            "h": max(float(item["h"]) for item in rows),
            "l": min(float(item["l"]) for item in rows),
            "c": float(rows[-1]["c"]),
            "v": sum(float(item.get("v") or 0.0) for item in rows),
        })
    return result[-limit:]


async def load_gold_live(client: httpx.AsyncClient) -> dict[str, Any]:
    """Refresh recent gold chart bars; this provider is a reference feed, not a broker quote."""
    one_minute = await _gold_chart(client, "5d", "1m")
    if not one_minute:
        raise RuntimeError("Gold live chart returned no recent 1m candles")
    data = {"1m": one_minute}
    for tf in ("5m", "15m", "30m", "1h", "4h", "1d"):
        # Rebuild only from the recent 1m window. The caller merges these
        # updates into the longer native-interval history loaded at startup.
        data[tf] = resample_from_1m(one_minute, tf, 10_000)
    return {"candles": data, "latest": one_minute[-1]}
