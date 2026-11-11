import time
import unittest

from unittest.mock import patch

from historical_sources import (
    _normalise_yahoo_chart,
    _resample_from_source,
    load_btc_um_history,
    resample_from_1m,
)


class HistoricalSourceTests(unittest.TestCase):
    def test_yahoo_payload_is_normalised_and_null_rows_skipped(self):
        payload = {
            "chart": {
                "error": None,
                "result": [{
                    "timestamp": [1_700_000_000, 1_700_000_060],
                    "indicators": {
                        "quote": [{
                            "open": [1980.0, None],
                            "high": [1982.0, None],
                            "low": [1979.0, None],
                            "close": [1981.0, None],
                            "volume": [1234, None],
                        }]
                    },
                }],
            }
        }
        candles = _normalise_yahoo_chart(payload, "1m")
        self.assertEqual(len(candles), 1)
        self.assertEqual(candles[0]["t"], 1_700_000_000_000)
        self.assertEqual(candles[0]["c"], 1981.0)

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

    def test_1h_resampler_rejects_partial_four_hour_bucket(self):
        now_ms = int(time.time() * 1000)
        bucket = ((now_ms // 14_400_000) - 1) * 14_400_000
        rows = [
            {"t": bucket + i * 3_600_000, "o": 100+i, "h": 102+i,
             "l": 99+i, "c": 101+i, "v": 1}
            for i in range(4)
        ]
        result = _resample_from_source(rows, "4h", 100)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["t"], bucket)


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
