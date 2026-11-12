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
BINANCE_FUTURES_REST = "https://fapi.binance.com"
BINANCE_TRADFI_GOLD_PAIR = "XAUUSDT"
BINANCE_TRADFI_GOLD_CONTRACT = "TRADIFI_PERPETUAL"
BINANCE_TRADFI_GOLD_LISTING = datetime(2026, 1, 5, tzinfo=timezone.utc)
BINANCE_FUTURES_KLINE_LIMIT = 1500

TIMEFRAMES_GOLD = ("1m", "5m", "15m", "30m", "1h", "4h", "1d")

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
    expected = duration // TF_MS["1m"]
    grouped: dict[int, list[dict[str, Any]]] = {}
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    for candle in candles:
        bucket = (int(candle["t"]) // duration) * duration
        if bucket + duration > now_ms:
            continue
        grouped.setdefault(bucket, []).append(candle)

    result = []
    for bucket, rows in sorted(grouped.items()):
        if len(rows) < expected:
            continue  # Do not manufacture a complete HTF bar from missing 1m candles.
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


async def _load_interval_archive(
    client: httpx.AsyncClient,
    symbol: str,
    timeframe: str,
    completed_months: int,
    limit: int,
    semaphore: asyncio.Semaphore | None = None,
) -> list[dict[str, Any]]:
    """Load a longer native-interval archive plus this month's published daily files."""
    now = datetime.now(timezone.utc)
    urls = []
    for month in _previous_months(now, completed_months):
        label = month.strftime("%Y-%m")
        name = f"{symbol}-{timeframe}-{label}.zip"
        urls.append(f"{BINANCE_ARCHIVE}/monthly/klines/{symbol}/{timeframe}/{name}")

    day = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    while day.date() < now.date():
        label = day.strftime("%Y-%m-%d")
        name = f"{symbol}-{timeframe}-{label}.zip"
        urls.append(f"{BINANCE_ARCHIVE}/daily/klines/{symbol}/{timeframe}/{name}")
        day += timedelta(days=1)

    semaphore = semaphore or asyncio.Semaphore(6)
    pages = await asyncio.gather(*(_download_archive(client, url, semaphore) for url in urls))
    by_time: dict[int, dict[str, Any]] = {}
    for page in pages:
        for candle in page:
            by_time[int(candle["t"])] = candle
    return [by_time[key] for key in sorted(by_time)][-limit:]


async def load_btc_um_history(
    client: httpx.AsyncClient,
    symbol: str = "BTCUSDT",
    limit: int = 25_000,
) -> dict[str, Any]:
    """Load deep native Binance USD-M OHLC archives independently for every timeframe."""
    now = datetime.now(timezone.utc)
    urls = []
    for month in _previous_months(now, 2):
        label = month.strftime("%Y-%m")
        name = f"{symbol}-1m-{label}.zip"
        urls.append(f"{BINANCE_ARCHIVE}/monthly/klines/{symbol}/1m/{name}")

    # Binance daily archives are normally published on the following day.
    day = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    while day.date() < now.date():
        label = day.strftime("%Y-%m-%d")
        name = f"{symbol}-1m-{label}.zip"
        urls.append(f"{BINANCE_ARCHIVE}/daily/klines/{symbol}/1m/{name}")
        day += timedelta(days=1)

    one_minute_semaphore = asyncio.Semaphore(4)
    pages = await asyncio.gather(
        *(_download_archive(client, url, one_minute_semaphore) for url in urls)
    )
    by_time: dict[int, dict[str, Any]] = {}
    for page in pages:
        for candle in page:
            by_time[int(candle["t"])] = candle
    one_minute = [by_time[key] for key in sorted(by_time)]
    if len(one_minute) < 30:
        raise RuntimeError(f"Binance public archive returned only {len(one_minute)} BTC 1m bars")

    # Request enough native monthly archives to fill the 25,000-bar replay cap
    # wherever BTC USD-M archive history exists. This prevents 1h/4h/1d history
    # from being artificially limited to the recent 1m download window.
    archive_months = {
        "5m": 4,
        "15m": 10,
        "30m": 20,
        "1h": 36,
        "4h": 96,
        "1d": 96,
    }
    archive_semaphore = asyncio.Semaphore(6)
    interval_names = list(archive_months)
    interval_pages = await asyncio.gather(
        *(
            _load_interval_archive(
                client, symbol, timeframe, archive_months[timeframe], limit,
                semaphore=archive_semaphore,
            )
            for timeframe in interval_names
        )
    )
    native_history = dict(zip(interval_names, interval_pages))

    data = {"1m": one_minute[-limit:]}
    for timeframe in interval_names:
        # Native archives are preferred. Resampling is only a fallback if the
        # provider has no native interval archive available.
        data[timeframe] = native_history.get(timeframe) or resample_from_1m(
            one_minute, timeframe, limit
        )

    counts = {tf: len(rows) for tf, rows in data.items()}
    log.info("BTC historical archive loaded: %s", counts)
    return {
        "candles": data,
        "source": "Binance USD-M BTCUSDT native timeframe archives (1m fallback resampling)",
        "bars": counts,
    }

def _normalise_binance_continuous_klines(payload: Any, timeframe: str) -> list[dict[str, Any]]:
    """Normalise Binance USD-M continuous-contract kline tuples, keeping closed bars only."""
    current_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    candles: dict[int, dict[str, Any]] = {}
    if not isinstance(payload, list):
        return []

    for row in payload:
        if not isinstance(row, (list, tuple)) or len(row) < 7:
            continue
        try:
            opened = int(row[0])
            closed = int(row[6])
            if opened <= 0 or closed < opened or closed >= current_ms:
                continue
            o, h, low, close = (float(row[index]) for index in (1, 2, 3, 4))
            volume = float(row[5] or 0.0)
            if min(o, h, low, close) <= 0 or h < max(o, low, close) or low > min(o, h, close):
                continue
            candles[opened] = {
                "t": opened, "o": o, "h": h, "l": low,
                "c": close, "v": max(0.0, volume),
            }
        except (TypeError, ValueError, IndexError, OverflowError):
            continue

    return [candles[key] for key in sorted(candles)]


async def _load_binance_tradfi_gold_interval(
    client: httpx.AsyncClient,
    timeframe: str,
    limit: int,
    semaphore: asyncio.Semaphore,
) -> list[dict[str, Any]]:
    """Page backward through native XAUUSDT TradFi perpetual candles (max 1,500/page)."""
    wanted = max(1, int(limit))
    by_time: dict[int, dict[str, Any]] = {}
    end_time: int | None = None

    async with semaphore:
        while len(by_time) < wanted:
            page_limit = min(BINANCE_FUTURES_KLINE_LIMIT, wanted - len(by_time))
            params: dict[str, Any] = {
                "pair": BINANCE_TRADFI_GOLD_PAIR,
                "contractType": BINANCE_TRADFI_GOLD_CONTRACT,
                "interval": timeframe,
                "limit": page_limit,
            }
            if end_time is not None:
                params["endTime"] = end_time

            response = await client.get(
                f"{BINANCE_FUTURES_REST}/fapi/v1/continuousKlines",
                params=params,
                timeout=18.0,
            )
            response.raise_for_status()
            raw_rows = response.json()
            if not isinstance(raw_rows, list) or not raw_rows:
                break

            page = _normalise_binance_continuous_klines(raw_rows, timeframe)
            for candle in page:
                by_time[int(candle["t"])] = candle

            try:
                oldest_open = min(int(row[0]) for row in raw_rows if isinstance(row, (list, tuple)) and row)
            except (TypeError, ValueError):
                break
            next_end_time = oldest_open - 1
            if end_time is not None and next_end_time >= end_time:
                break
            end_time = next_end_time

            if len(raw_rows) < page_limit:
                break

    return [by_time[key] for key in sorted(by_time)][-wanted:]


async def _load_binance_tradfi_gold_archive(
    client: httpx.AsyncClient,
    limit: int = 25_000,
) -> dict[str, Any]:
    """Backfill XAUUSDT from Binance USD-M public archives from its Jan-2026 listing.

    The archive stores symbol klines. The 1m series is retained in full temporarily
    to build complete higher timeframes; the dashboard keeps the standard 25k 1m
    replay window, matching the BTC history capacity.
    """
    now = datetime.now(timezone.utc)
    completed_months = max(
        0, (now.year - BINANCE_TRADFI_GOLD_LISTING.year) * 12
        + now.month - BINANCE_TRADFI_GOLD_LISTING.month,
    )
    full_minute_limit = max(
        limit,
        int((now - BINANCE_TRADFI_GOLD_LISTING).total_seconds() // 60) + 2_000,
    )
    minute_rows = await _load_interval_archive(
        client,
        BINANCE_TRADFI_GOLD_PAIR,
        "1m",
        completed_months,
        full_minute_limit,
        semaphore=asyncio.Semaphore(6),
    )
    if len(minute_rows) < 25:
        raise RuntimeError(
            "Binance XAUUSDT public 1m archive has fewer than 25 closed candles; "
            "no non-Binance Gold fallback is configured"
        )

    data = {"1m": minute_rows[-limit:]}
    for timeframe in ("5m", "15m", "30m", "1h", "4h", "1d"):
        data[timeframe] = resample_from_1m(minute_rows, timeframe, limit)

    counts = {tf: len(rows) for tf, rows in data.items()}
    if counts["1d"] < 25:
        raise RuntimeError(
            f"Binance XAUUSDT archive produced only {counts['1d']} daily candles; "
            "not enough history for complete multi-timeframe Gold analysis"
        )
    log.info("Binance XAUUSDT archive backfill loaded from TradFi listing: %s", counts)
    return {
        "candles": data,
        "source": (
            "Binance Futures TradFi Perpetual XAUUSDT public archive "
            "(native 1m; higher timeframes resampled from Binance 1m)"
        ),
        "bars": counts,
    }


async def load_binance_tradfi_gold_history(
    client: httpx.AsyncClient,
    limit: int = 25_000,
) -> dict[str, Any]:
    """Load Gold only from Binance continuousKlines, then Binance public archives."""
    limits = {tf: min(max(1, limit), 25_000) for tf in TIMEFRAMES_GOLD}
    semaphore = asyncio.Semaphore(3)

    async def fetch_one(timeframe: str, wanted: int) -> tuple[str, list[dict[str, Any]]]:
        try:
            rows = await _load_binance_tradfi_gold_interval(client, timeframe, wanted, semaphore)
            return timeframe, rows
        except (httpx.HTTPError, ValueError, TypeError, RuntimeError) as exc:
            log.warning("Binance TradFi GOLD %s REST history unavailable: %s", timeframe, exc)
            return timeframe, []

    fetched = await asyncio.gather(*(fetch_one(tf, wanted) for tf, wanted in limits.items()))
    data = {tf: rows for tf, rows in fetched}
    source = (
        "Binance Futures TradFi Perpetual XAUUSDT continuousKlines "
        "(native intervals)"
    )

    if any(len(data.get(tf, [])) < 25 for tf in ("1m", "1d", "4h")):
        log.warning("Binance XAUUSDT continuousKlines REST history is incomplete; trying Binance public archives")
        archived = await _load_binance_tradfi_gold_archive(client, limit)
        archive_data = archived["candles"]
        for timeframe in TIMEFRAMES_GOLD:
            if len(data.get(timeframe, [])) < 25:
                data[timeframe] = archive_data.get(timeframe, [])
        source = archived["source"]

    if len(data.get("1m", [])) < 25:
        raise RuntimeError("Binance XAUUSDT returned fewer than 25 completed 1m candles")

    # Preserve native Binance intervals whenever available; derive only missing
    # intervals from the same Binance 1m source, never from a different instrument.
    full_minute_rows = list(data["1m"])
    for timeframe in ("5m", "15m", "30m", "1h", "4h", "1d"):
        if len(data.get(timeframe, [])) < 25:
            data[timeframe] = resample_from_1m(full_minute_rows, timeframe, limit)

    counts = {tf: len(rows) for tf, rows in data.items()}
    log.info("Binance TradFi XAUUSDT history loaded: %s", counts)
    return {"candles": {tf: rows[-limit:] for tf, rows in data.items()}, "source": source, "bars": counts}


async def load_binance_tradfi_gold_live(client: httpx.AsyncClient) -> dict[str, Any]:
    """Fetch the latest closed native XAUUSDT TradFi bars when WebSocket is unavailable."""
    semaphore = asyncio.Semaphore(4)
    timeframes = ("1m", "5m", "15m", "30m", "1h", "4h", "1d")
    fetched = await asyncio.gather(*(
        _load_binance_tradfi_gold_interval(client, tf, 3, semaphore)
        for tf in timeframes
    ))
    data = {tf: rows for tf, rows in zip(timeframes, fetched)}
    one_minute = data.get("1m", [])
    if not one_minute:
        raise RuntimeError("Binance XAUUSDT TradFi REST refresh returned no closed 1m candles")
    return {
        "candles": data,
        "latest": one_minute[-1],
        "source": "Binance Futures TradFi Perpetual XAUUSDT continuousKlines",
    }
