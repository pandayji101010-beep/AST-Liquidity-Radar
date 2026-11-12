import time
import unittest

from unittest.mock import AsyncMock, patch

from historical_sources import (
    load_btc_um_history,
    load_binance_tradfi_gold_history,
    _normalise_binance_continuous_klines,
    resample_from_1m,
)


class HistoricalSourceTests(unittest.IsolatedAsyncioTestCase):
    async def test_binance_archive_is_used_when_continuous_rest_is_blocked(self):
        now_ms = int(time.time() * 1000)
        start = ((now_ms - 27 * 86_400_000) // 86_400_000) * 86_400_000
        minute_rows = [
            {"t": start + index * 60_000, "o": 2350.0, "h": 2352.0,
             "l": 2349.0, "c": 2351.0, "v": 1.0}
            for index in range(26 * 24 * 60)
        ]
        archive_candles = {"1m": minute_rows[-25_000:]}
        for timeframe in ("5m", "15m", "30m", "1h", "4h", "1d"):
            archive_candles[timeframe] = resample_from_1m(minute_rows, timeframe, 25_000)
        archive_payload = {
            "candles": archive_candles,
            "source": "Binance Futures TradFi Perpetual XAUUSDT public archive",
            "bars": {tf: len(rows) for tf, rows in archive_candles.items()},
        }

        async def no_rest(*args, **kwargs):
            return []

        with patch("historical_sources._load_binance_tradfi_gold_interval", new=AsyncMock(side_effect=no_rest)), patch(
            "historical_sources._load_binance_tradfi_gold_archive",
            new=AsyncMock(return_value=archive_payload),
        ) as archive:
            result = await load_binance_tradfi_gold_history(object(), limit=25_000)
        archive.assert_awaited_once()
        self.assertIn("Binance Futures TradFi Perpetual XAUUSDT public archive", result["source"])
        self.assertEqual(result["bars"]["1m"], 25_000)
        self.assertGreaterEqual(result["bars"]["1d"], 25)

    def test_binance_tradfi_continuous_klines_keep_only_valid_closed_xauusdt_bars(self):
        now_ms = int(time.time() * 1000)
        closed_open = ((now_ms - 180_000) // 60_000) * 60_000
        active_open = (now_ms // 60_000) * 60_000
        payload = [
            [closed_open, "2350.0", "2352.0", "2349.0", "2351.5", "12.5",
             closed_open + 59_999, "29393.75", 42, "6.0", "14095.0", "0"],
            [active_open, "2351.5", "2353.0", "2351.0", "2352.5", "4.0",
             active_open + 59_999, "9400.0", 8, "2.0", "4700.0", "0"],
        ]
        candles = _normalise_binance_continuous_klines(payload, "1m")
        self.assertEqual(len(candles), 1)
        self.assertEqual(candles[0]["t"], closed_open)
        self.assertEqual(candles[0]["o"], 2350.0)
        self.assertEqual(candles[0]["c"], 2351.5)
        self.assertEqual(candles[0]["v"], 12.5)

    async def test_binance_tradfi_gold_history_requests_xauusdt_continuous_contract(self):
        now_ms = int(time.time() * 1000)
        calls = []

        class FakeResponse:
            def __init__(self, payload):
                self.payload = payload

            def raise_for_status(self):
                return None

            def json(self):
                return self.payload

        class FakeClient:
            async def get(self, url, params, timeout):
                calls.append((url, dict(params)))
                timeframe = params["interval"]
                duration = {
                    "1m": 60_000, "5m": 300_000, "15m": 900_000,
                    "30m": 1_800_000, "1h": 3_600_000,
                    "4h": 14_400_000, "1d": 86_400_000,
                }[timeframe]
                opened = ((now_ms - duration * 40) // duration) * duration
                payload = [
                    [
                        opened + index * duration, "2350.0", "2352.0", "2349.0", "2351.5", "12.5",
                        opened + (index + 1) * duration - 1, "29393.75", 42, "6.0", "14095.0", "0",
                    ]
                    for index in range(30)
                ]
                return FakeResponse(payload)

        result = await load_binance_tradfi_gold_history(FakeClient(), limit=10_000)
        self.assertEqual(set(result["candles"]), {"1m", "5m", "15m", "30m", "1h", "4h", "1d"})
        self.assertIn("Binance Futures TradFi Perpetual XAUUSDT", result["source"])
        self.assertEqual(result["bars"]["1m"], 30)
        self.assertTrue(calls)
        self.assertTrue(all(params["pair"] == "XAUUSDT" for _url, params in calls))
        self.assertTrue(all(params["contractType"] == "TRADIFI_PERPETUAL" for _url, params in calls))
        self.assertEqual(
            {params["interval"] for _url, params in calls},
            {"1m", "5m", "15m", "30m", "1h", "4h", "1d"},
        )

    def test_1m_resampler_rejects_partial_five_minute_bucket(self):
        now_ms = int(time.time() * 1000)
        bucket = ((now_ms - 600_000) // 300_000) * 300_000
        rows = [
            {"t": bucket + i * 60_000, "o": 100+i, "h": 102+i,
             "l": 99+i, "c": 101+i, "v": 1}
            for i in range(5)
        ]
        rows += [
            {"t": bucket + 300_000 + i * 60_000, "o": 110+i, "h": 112+i,
             "l": 109+i, "c": 111+i, "v": 1}
            for i in range(2)
        ]
        result = resample_from_1m(rows, "5m", 100)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["t"], bucket)
        self.assertEqual(result[0]["v"], 5)




class BtcNativeHistoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_btc_history_requests_native_archive_for_each_timeframe(self):
        requested_months = {}

        async def fake_download(client, url, semaphore):
            return [
                {
                    "t": 1_700_000_000_000 + index * 60_000,
                    "o": 100.0,
                    "h": 101.0,
                    "l": 99.0,
                    "c": 100.5,
                    "v": 1.0,
                }
                for index in range(30)
            ]

        async def fake_native_history(
            client, symbol, timeframe, completed_months, limit, semaphore=None
        ):
            requested_months[timeframe] = completed_months
            return [{
                "t": 1_700_000_000_000,
                "o": 100.0,
                "h": 101.0,
                "l": 99.0,
                "c": 100.5,
                "v": 1.0,
            }]

        with patch("historical_sources._download_archive", side_effect=fake_download), patch(
            "historical_sources._load_interval_archive",
            side_effect=fake_native_history,
        ):
            result = await load_btc_um_history(object(), limit=100)

        self.assertEqual(
            set(result["candles"]),
            {"1m", "5m", "15m", "30m", "1h", "4h", "1d"},
        )
        self.assertEqual(
            requested_months,
            {"5m": 4, "15m": 10, "30m": 20, "1h": 36, "4h": 96, "1d": 96},
        )
        self.assertEqual(result["bars"]["1m"], 30)
        for timeframe in ("5m", "15m", "30m", "1h", "4h", "1d"):
            self.assertEqual(result["bars"][timeframe], 1)


if __name__ == "__main__":
    unittest.main()
