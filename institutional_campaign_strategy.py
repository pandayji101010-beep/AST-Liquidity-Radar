from __future__ import annotations

import bisect
import math
import os
from datetime import datetime, timedelta, timezone
from typing import Any


FRAME_MS = {
    "1m": 60_000, "5m": 300_000, "15m": 900_000,
    "30m": 1_800_000, "1h": 3_600_000, "4h": 14_400_000,
    "1d": 86_400_000,
}
HTF_ORDER = ("1M", "1w", "1d", "4h", "1h", "30m", "15m", "5m")
HTF_WEIGHTS = {
    "1M": 1.0, "1w": 1.5, "1d": 2.0, "4h": 2.0,
    "1h": 2.5, "30m": 2.5, "15m": 3.0, "5m": 3.0,
}


def _env_float(name: str, default: float) -> float:
    try:
        value = float(os.getenv(name, str(default)))
        return value if math.isfinite(value) and value > 0 else default
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        return default


class ASTInstitutionalCampaignStrategy:
    """Point-in-time multi-timeframe campaign scanner and OHLC replay.

    Higher-timeframe candles are visible only after their close timestamp.
    Signals are emitted on a closed 1m bar and simulated at the next 1m open.
    This creates plans and replay records; it does not send broker orders.
    """

    REPLAY_LIMIT = _env_int("AST_CAMPAIGN_REPLAY_BARS", 25_000)
    PER_POSITION_RISK_PCT = _env_float("AST_CAMPAIGN_ENTRY_RISK_PCT", 0.20)
    MAX_CAMPAIGN_RISK_PCT = _env_float("AST_CAMPAIGN_MAX_RISK_PCT", 1.50)
    DAILY_MAX_LOSS_PCT = _env_float("AST_CAMPAIGN_DAILY_LOSS_PCT", 2.00)
    MAX_POSITIONS = _env_int("AST_CAMPAIGN_MAX_POSITIONS", 10)
    MIN_TARGET_R = 1.50
    MIN_SETUP_SCORE = 70.0

    def __init__(self) -> None:
        self._cache_key: tuple[Any, ...] | None = None
        self._cache_value: dict[str, Any] | None = None

    @staticmethod
    def _clean_bars(source: Any) -> list[dict[str, float | int]]:
        by_time: dict[int, dict[str, float | int]] = {}
        if not source:
            return []
        for row in source:
            try:
                ts = int(row.get("t", 0))
                o, h, low, c = (float(row[k]) for k in ("o", "h", "l", "c"))
                volume = float(row.get("v") or 0.0)
                if ts <= 0 or min(o, h, low, c) <= 0:
                    continue
                if h < max(o, c, low) or low > min(o, c, h):
                    continue
                if not all(math.isfinite(x) for x in (o, h, low, c, volume)):
                    continue
                by_time[ts] = {"t": ts, "o": o, "h": h, "l": low, "c": c, "v": volume}
            except (TypeError, ValueError, KeyError, AttributeError):
                continue
        return [by_time[ts] for ts in sorted(by_time)]

    @staticmethod
    def _ema(values: list[float], period: int) -> float | None:
        if not values:
            return None
        alpha = 2.0 / (period + 1.0)
        result = values[0]
        for value in values[1:]:
            result = alpha * value + (1.0 - alpha) * result
        return result

    @staticmethod
    def _atr(bars: list[dict[str, Any]], period: int = 14) -> float:
        if len(bars) < 2:
            return 0.0
        ranges = []
        start = max(1, len(bars) - period)
        for i in range(start, len(bars)):
            prev = float(bars[i - 1]["c"])
            ranges.append(max(
                float(bars[i]["h"]) - float(bars[i]["l"]),
                abs(float(bars[i]["h"]) - prev),
                abs(float(bars[i]["l"]) - prev),
            ))
        return sum(ranges) / len(ranges) if ranges else 0.0

    @staticmethod
    def _aggregate_calendar(
        daily: list[dict[str, Any]], period: str
    ) -> list[dict[str, Any]]:
        groups: dict[int, list[dict[str, Any]]] = {}
        for bar in daily:
            dt = datetime.fromtimestamp(int(bar["t"]) / 1000, tz=timezone.utc)
            if period == "1w":
                start_date = dt.date() - timedelta(days=dt.weekday())
                start = datetime(start_date.year, start_date.month, start_date.day, tzinfo=timezone.utc)
                end = start + timedelta(days=7)
            else:
                start = datetime(dt.year, dt.month, 1, tzinfo=timezone.utc)
                if dt.month == 12:
                    end = datetime(dt.year + 1, 1, 1, tzinfo=timezone.utc)
                else:
                    end = datetime(dt.year, dt.month + 1, 1, tzinfo=timezone.utc)
            bucket = int(start.timestamp() * 1000)
            groups.setdefault(bucket, []).append(bar)

        result = []
        for bucket, rows in sorted(groups.items()):
            rows.sort(key=lambda item: int(item["t"]))
            last = rows[-1]
            dt = datetime.fromtimestamp(bucket / 1000, tz=timezone.utc)
            if period == "1w":
                end_dt = dt + timedelta(days=7)
            elif dt.month == 12:
                end_dt = datetime(dt.year + 1, 1, 1, tzinfo=timezone.utc)
            else:
                end_dt = datetime(dt.year, dt.month + 1, 1, tzinfo=timezone.utc)
            result.append({
                "t": bucket,
                "o": float(rows[0]["o"]),
                "h": max(float(item["h"]) for item in rows),
                "l": min(float(item["l"]) for item in rows),
                "c": float(last["c"]),
                "v": sum(float(item.get("v") or 0.0) for item in rows),
                "_close_t": int(end_dt.timestamp() * 1000),
            })
        return result

    @staticmethod
    def _frame_snapshot(bars: list[dict[str, Any]], timeframe: str) -> dict[str, Any]:
        minimum = 8 if timeframe == "1M" else 12 if timeframe == "1w" else 25
        if len(bars) < minimum:
            return {
                "timeframe": timeframe, "ready": False, "bars": len(bars),
                "side": None, "trend": "WARMING UP",
                "reason": f"Need {minimum} completed candles; have {len(bars)}.",
            }

        closes = [float(x["c"]) for x in bars]
        highs = [float(x["h"]) for x in bars]
        lows = [float(x["l"]) for x in bars]
        last, previous = bars[-1], bars[-2]
        close = closes[-1]
        ema20 = ASTInstitutionalCampaignStrategy._ema(closes[-min(len(closes), 240):], 20)
        ema50 = ASTInstitutionalCampaignStrategy._ema(closes[-min(len(closes), 480):], 50)
        atr = ASTInstitutionalCampaignStrategy._atr(bars)
        prior = bars[-21:-1] if len(bars) >= 21 else bars[:-1]
        prior_high_bar = max(prior, key=lambda x: float(x["h"])) if prior else None
        prior_low_bar = min(prior, key=lambda x: float(x["l"])) if prior else None
        prior_high = float(prior_high_bar["h"]) if prior_high_bar else None
        prior_low = float(prior_low_bar["l"]) if prior_low_bar else None
        prior_high_time = int(prior_high_bar["t"]) if prior_high_bar else None
        prior_low_time = int(prior_low_bar["t"]) if prior_low_bar else None

        swing_high = None
        swing_low = None
        pivot_high_time = None
        pivot_low_time = None
        confirmed_highs: list[float] = []
        confirmed_lows: list[float] = []
        pivot_start = max(2, len(bars) - 100)
        for i in range(pivot_start, len(bars) - 2):
            left_h = highs[i - 2:i]
            right_h = highs[i + 1:i + 3]
            left_l = lows[i - 2:i]
            right_l = lows[i + 1:i + 3]
            if len(left_h) == 2 and len(right_h) == 2:
                if highs[i] > max(left_h) and highs[i] >= max(right_h):
                    swing_high, pivot_high_time = highs[i], int(bars[i]["t"])
                    confirmed_highs.append(highs[i])
                if lows[i] < min(left_l) and lows[i] <= min(right_l):
                    swing_low, pivot_low_time = lows[i], int(bars[i]["t"])
                    confirmed_lows.append(lows[i])

        equal_tolerance = max(atr * 0.20, close * 0.00015)
        equal_high = (
            (confirmed_highs[-1] + confirmed_highs[-2]) / 2.0
            if len(confirmed_highs) >= 2 and abs(confirmed_highs[-1] - confirmed_highs[-2]) <= equal_tolerance
            else None
        )
        equal_low = (
            (confirmed_lows[-1] + confirmed_lows[-2]) / 2.0
            if len(confirmed_lows) >= 2 and abs(confirmed_lows[-1] - confirmed_lows[-2]) <= equal_tolerance
            else None
        )
        previous_period_high = float(last["h"])
        previous_period_low = float(last["l"])

        upper_sweep = bool(prior_high is not None and float(last["h"]) > prior_high and close < prior_high)
        lower_sweep = bool(prior_low is not None and float(last["l"]) < prior_low and close > prior_low)
        body = abs(close - float(last["o"]))
        full_range = max(float(last["h"]) - float(last["l"]), 1e-12)
        close_pos = (close - float(last["l"])) / full_range
        bull_displacement = bool(close > float(last["o"]) and atr > 0 and body >= atr * 0.80 and close_pos >= 0.70)
        bear_displacement = bool(close < float(last["o"]) and atr > 0 and body >= atr * 0.80 and close_pos <= 0.30)

        bos_side = None
        if swing_high is not None and close > swing_high and float(previous["c"]) <= swing_high:
            bos_side = "BUY"
        elif swing_low is not None and close < swing_low and float(previous["c"]) >= swing_low:
            bos_side = "SELL"

        displacement_side = "BUY" if bull_displacement else "SELL" if bear_displacement else None
        previous_closes = closes[:-1]
        previous_ema20 = ASTInstitutionalCampaignStrategy._ema(previous_closes[-min(len(previous_closes), 240):], 20)
        previous_ema50 = ASTInstitutionalCampaignStrategy._ema(previous_closes[-min(len(previous_closes), 480):], 50)
        previous_side = (
            "BUY" if previous_ema20 is not None and previous_ema50 is not None
            and float(previous["c"]) > previous_ema20 > previous_ema50 else
            "SELL" if previous_ema20 is not None and previous_ema50 is not None
            and float(previous["c"]) < previous_ema20 < previous_ema50 else None
        )
        structure_event = (
            "CHOCH" if bos_side and previous_side and previous_side != bos_side
            else "BOS" if bos_side else None
        )
        if ema20 is not None and ema50 is not None and close > ema20 and ema20 > ema50:
            side = "BUY"
        elif ema20 is not None and ema50 is not None and close < ema20 and ema20 < ema50:
            side = "SELL"
        else:
            side = bos_side

        # Detect a fresh, still-unfilled three-candle fair-value gap.
        fvg = None
        fvg_start = max(2, len(bars) - 25)
        for i in range(len(bars) - 1, fvg_start - 1, -1):
            if float(bars[i]["l"]) > float(bars[i - 2]["h"]):
                zone = (float(bars[i - 2]["h"]), float(bars[i]["l"]))
                later = bars[i + 1:]
                if not any(float(x["l"]) <= zone[0] for x in later):
                    fvg = {"side": "BUY", "low": zone[0], "high": zone[1], "time": int(bars[i]["t"])}
                    break
            if float(bars[i]["h"]) < float(bars[i - 2]["l"]):
                zone = (float(bars[i]["h"]), float(bars[i - 2]["l"]))
                later = bars[i + 1:]
                if not any(float(x["h"]) >= zone[1] for x in later):
                    fvg = {"side": "SELL", "low": zone[0], "high": zone[1], "time": int(bars[i]["t"])}
                    break

        # Candidate order block: last opposing candle before a strong recent impulse.
        order_block = None
        for i in range(len(bars) - 1, max(-1, len(bars) - 9), -1):
            candle = bars[i]
            candle_body = abs(float(candle["c"]) - float(candle["o"]))
            if atr <= 0 or candle_body < atr * 0.80:
                continue
            impulse_side = "BUY" if float(candle["c"]) > float(candle["o"]) else "SELL"
            for prior_bar in reversed(bars[max(0, i - 4):i]):
                opposite = (
                    impulse_side == "BUY" and float(prior_bar["c"]) < float(prior_bar["o"])
                ) or (
                    impulse_side == "SELL" and float(prior_bar["c"]) > float(prior_bar["o"])
                )
                if opposite:
                    lo = float(prior_bar["l"])
                    hi = float(prior_bar["h"])
                    order_block = {"side": impulse_side, "low": lo, "high": hi, "time": int(prior_bar["t"])}
                    break
            if order_block:
                break

        range_bars = bars[-50:]
        range_high = max(float(x["h"]) for x in range_bars)
        range_low = min(float(x["l"]) for x in range_bars)
        midpoint = (range_high + range_low) / 2.0
        premium_discount = "DISCOUNT" if close <= midpoint else "PREMIUM"
        sample_closes = closes[-21:]
        travelled = sum(abs(sample_closes[i] - sample_closes[i - 1]) for i in range(1, len(sample_closes)))
        net_move = abs(sample_closes[-1] - sample_closes[0]) if len(sample_closes) > 1 else 0.0
        efficiency_ratio = net_move / travelled if travelled > 0 else 0.0
        range_width_atr = (range_high - range_low) / atr if atr > 0 else None
        choppy = bool(efficiency_ratio < 0.12 and (range_width_atr is None or range_width_atr < 5.0))
        volumes = [float(x.get("v") or 0.0) for x in bars[-21:-1]]
        avg_volume = sum(volumes) / len(volumes) if volumes else 0.0
        rel_volume = float(last.get("v") or 0.0) / avg_volume if avg_volume > 0 else None
        utc_hour = datetime.fromtimestamp(int(last["t"]) / 1000, tz=timezone.utc).hour
        session_ok = 6 <= utc_hour < 21
        atr_pct = atr / close * 100.0 if close else None
        volatility_ok = atr_pct is not None and 0.02 <= atr_pct <= 2.5

        return {
            "timeframe": timeframe, "ready": True, "bars": len(bars),
            "side": side, "trend": "BULLISH" if side == "BUY" else "BEARISH" if side == "SELL" else "NEUTRAL",
            "close": close, "candle_time": int(last["t"]),
            "ema20": ema20, "ema50": ema50, "atr": atr, "atr_pct": atr_pct,
            "prior_high": prior_high, "prior_low": prior_low,
            "prior_high_time": prior_high_time, "prior_low_time": prior_low_time,
            "previous_period_high": previous_period_high, "previous_period_low": previous_period_low,
            "swing_high": swing_high, "swing_low": swing_low,
            "equal_high": equal_high, "equal_low": equal_low,
            "equal_high_time": pivot_high_time if equal_high is not None else None,
            "equal_low_time": pivot_low_time if equal_low is not None else None,
            "pivot_high_time": pivot_high_time, "pivot_low_time": pivot_low_time,
            "upper_sweep": upper_sweep, "lower_sweep": lower_sweep,
            "bos_side": bos_side, "structure_event": structure_event,
            "displacement_side": displacement_side,
            "bull_displacement": bull_displacement, "bear_displacement": bear_displacement,
            "fvg": fvg, "order_block": order_block,
            "premium_discount": premium_discount, "range_high": range_high, "range_low": range_low,
            "efficiency_ratio": efficiency_ratio, "range_width_atr": range_width_atr, "choppy": choppy,
            "relative_volume": rel_volume, "volume_confirmed": rel_volume is not None and rel_volume >= 1.20,
            "session_ok": session_ok, "volatility_ok": volatility_ok,
            "reason": "Closed-candle structure, trend, liquidity and imbalance snapshot.",
        }

    @staticmethod
    def _alignment(frames: dict[str, dict[str, Any]]) -> dict[str, Any]:
        ready = {tf: frames.get(tf, {}) for tf in HTF_ORDER}
        usable = {
            tf: data for tf, data in ready.items()
            if data.get("ready") and data.get("side") in ("BUY", "SELL")
        }
        buy_weight = sum(HTF_WEIGHTS[tf] for tf, data in usable.items() if data["side"] == "BUY")
        sell_weight = sum(HTF_WEIGHTS[tf] for tf, data in usable.items() if data["side"] == "SELL")
        total = buy_weight + sell_weight
        side = "BUY" if buy_weight > sell_weight else "SELL" if sell_weight > buy_weight else None
        ratio = max(buy_weight, sell_weight) / total if total else 0.0
        core_ready = all(ready.get(tf, {}).get("ready") for tf in ("1d", "4h", "1h", "15m", "5m"))
        near_term_aligned = bool(
            side and ready.get("15m", {}).get("side") == side and ready.get("5m", {}).get("side") == side
        )
        confirmed = bool(core_ready and near_term_aligned and ratio >= 0.65)
        status = "CONFIRMED" if confirmed else "CONFLICT" if side and not near_term_aligned else "WAITING"
        return {
            "side": side if confirmed else None,
            "candidate_side": side,
            "status": status,
            "agreement_pct": round(ratio * 100.0, 1),
            "buy_weight": round(buy_weight, 2),
            "sell_weight": round(sell_weight, 2),
            "ready_timeframes": [tf for tf in HTF_ORDER if ready.get(tf, {}).get("ready")],
            "reason": (
                "Weighted HTF bias confirmed; 15m and 5m agree on completed candles."
                if confirmed else
                "15m/5m must agree and at least 65% of the available directional weight must support one side."
            ),
        }

    @staticmethod
    def _setup_score(
        alignment: dict[str, Any], micro: dict[str, Any], side: str,
        has_zone: bool, price_in_discount: bool | None
    ) -> dict[str, Any]:
        checks = {
            "higher_timeframe_bias": 10 if alignment.get("side") == side else 0,
            "liquidity_sweep_or_rejection": 15 if micro.get("sweep_side") == side else 0,
            "smt_divergence": None,  # No independent correlated-symbol feed; do not fabricate SMT.
            "mss_or_bos": 15 if micro.get("bos_side") == side else 0,
            "displacement": 10 if micro.get("displacement_side") == side else 0,
            "fvg_or_order_block": 15 if has_zone else 0,
            "relative_volume": 10 if micro.get("volume_confirmed") else 0,
            "premium_discount": 5 if price_in_discount is True else 0,
            "session": 5 if micro.get("session_ok") else 0,
            "atr_filter": 5 if micro.get("volatility_ok") else 0,
        }
        available_max = 90  # SMT is unavailable until a valid correlated feed is configured.
        earned = sum(value for value in checks.values() if value is not None)
        score = round(earned / available_max * 100.0, 1)
        grade = "HIGHEST QUALITY" if score >= 90 else "STRONG" if score >= 80 else "MODERATE" if score >= 70 else "NO TRADE"
        return {"score": score, "grade": grade, "checks": checks, "available_points": available_max}

    @staticmethod
    def _target_for(
        side: str, entry: float, stop: float, frames: dict[str, dict[str, Any]]
    ) -> tuple[float | None, float | None]:
        risk = abs(entry - stop)
        if risk <= 0:
            return None, None
        candidates: set[float] = set()
        for timeframe in ("5m", "15m", "30m", "1h", "4h", "1d", "1w", "1M"):
            data = frames.get(timeframe, {})
            if not data.get("ready"):
                continue
            for key in (
                "prior_high", "prior_low", "previous_period_high", "previous_period_low",
                "equal_high", "equal_low", "swing_high", "swing_low", "range_high", "range_low",
            ):
                value = data.get(key)
                if isinstance(value, (int, float)) and math.isfinite(value):
                    candidates.add(float(value))
        if side == "BUY":
            eligible = sorted(x for x in candidates if x > entry and (x - entry) / risk >= ASTInstitutionalCampaignStrategy.MIN_TARGET_R)
            target = eligible[0] if eligible else None
        else:
            eligible = sorted((x for x in candidates if x < entry and (entry - x) / risk >= ASTInstitutionalCampaignStrategy.MIN_TARGET_R), reverse=True)
            target = eligible[0] if eligible else None
        return target, ((abs(target - entry) / risk) if target is not None else None)

    def _cache_signature(self, candles: dict[str, Any], asset: str) -> tuple[Any, ...]:
        signature: list[Any] = [asset]
        for tf in ("1m", "5m", "15m", "30m", "1h", "4h", "1d"):
            rows = candles.get(tf) or []
            if rows:
                first = rows[0]
                last = rows[-1]
                signature.append((tf, len(rows), int(first["t"]), int(last["t"]), float(last["c"])))
            else:
                signature.append((tf, 0, None, None, None))
        return tuple(signature)

    def snapshot(self, candles: dict[str, Any], asset: str = "MARKET") -> dict[str, Any]:
        key = self._cache_signature(candles, asset)
        if self._cache_key == key and self._cache_value is not None:
            return self._cache_value
        result = self._replay(candles, asset)
        self._cache_key, self._cache_value = key, result
        return result

    def _replay(self, candles: dict[str, Any], asset: str) -> dict[str, Any]:
        one = self._clean_bars(candles.get("1m"))
        if not one:
            return {
                "asset": asset, "status": "WAITING FOR 1M DATA", "higher_timeframes": {},
                "alignment": {"status": "WAITING", "side": None, "agreement_pct": 0},
                "execution_1m": None, "campaign": {"status": "NO CAMPAIGN", "positions": []},
                "replay": {"bars_processed": 0, "lookahead_safe": True},
                "reason": "No completed 1m candles are available.",
            }

        one = one[-self.REPLAY_LIMIT:]
        source_frames: dict[str, list[dict[str, Any]]] = {
            tf: self._clean_bars(candles.get(tf)) for tf in FRAME_MS if tf != "1m"
        }
        source_frames["1w"] = self._aggregate_calendar(source_frames.get("1d", []), "1w")
        source_frames["1M"] = self._aggregate_calendar(source_frames.get("1d", []), "1M")
        source_frames["1m"] = one

        close_times: dict[str, list[int]] = {}
        for tf, rows in source_frames.items():
            if tf in ("1w", "1M"):
                close_times[tf] = [int(x["_close_t"]) for x in rows]
            else:
                duration = FRAME_MS[tf]
                close_times[tf] = [int(x["t"]) + duration for x in rows]

        pointers = {tf: 0 for tf in source_frames}
        cached_count = {tf: -1 for tf in source_frames}
        cached_frame: dict[str, dict[str, Any]] = {}
        higher: dict[str, dict[str, Any]] = {}
        campaign: dict[str, Any] | None = None
        pending: dict[str, Any] | None = None
        setup: dict[str, Any] | None = None
        last_signal: dict[str, Any] | None = None
        recent_closed: list[dict[str, Any]] = []
        closed_history: list[dict[str, Any]] = []
        campaign_entry_sizes: list[int] = []
        campaign_count = 0
        latest_alignment: dict[str, Any] = {"status": "WAITING", "side": None, "agreement_pct": 0}
        daily_pnl: dict[str, float] = {}
        latest_micro: dict[str, Any] = {}
        processed = 0
        previous_day = None

        def record_closed(position: dict[str, Any]) -> None:
            item = {k: position.get(k) for k in (
                "id", "kind", "side", "entry_price", "stop_loss", "take_profit",
                "status", "result_r", "entry_time", "exit_time", "risk_pct",
            )}
            recent_closed.append(item)
            closed_history.append(item)

        for index, bar in enumerate(one):
            asof = int(bar["t"]) + FRAME_MS["1m"]
            day_key = datetime.fromtimestamp(int(bar["t"]) / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
            previous_day = day_key

            for tf, rows in source_frames.items():
                if tf == "1m":
                    continue
                count = bisect.bisect_right(close_times[tf], asof)
                pointers[tf] = count
                if count != cached_count[tf]:
                    available = rows[max(0, count - 320):count]
                    # Use only closed bars; for calendar frames, their completed period end is the close time.
                    if tf in ("1w", "1M"):
                        available = [x for x in available if int(x["_close_t"]) <= asof]
                    else:
                        available = [x for x in available if int(x["t"]) + FRAME_MS[tf] <= asof]
                    cached_frame[tf] = self._frame_snapshot(available, tf)
                    cached_count[tf] = count

            higher = {tf: cached_frame.get(tf, {"ready": False, "side": None, "trend": "WARMING UP"}) for tf in HTF_ORDER}
            latest_alignment = self._alignment(higher)
            micro_bars = one[max(0, index - 320):index + 1]
            micro_frame = self._frame_snapshot(micro_bars, "1m")
            latest_micro = micro_frame
            if not micro_frame.get("ready"):
                processed += 1
                continue

            # Confirm 1m pivots with two completed bars to the right; no future candles are consulted.
            prior_high = micro_frame.get("prior_high")
            prior_low = micro_frame.get("prior_low")
            current = bar
            body = abs(float(current["c"]) - float(current["o"]))
            full_range = max(float(current["h"]) - float(current["l"]), 1e-12)
            close_pos = (float(current["c"]) - float(current["l"])) / full_range
            atr = float(micro_frame.get("atr") or 0.0)
            lower_sweep = bool(prior_low is not None and float(current["l"]) < prior_low and float(current["c"]) > prior_low)
            upper_sweep = bool(prior_high is not None and float(current["h"]) > prior_high and float(current["c"]) < prior_high)
            lower_wick = min(float(current["o"]), float(current["c"])) - float(current["l"])
            upper_wick = float(current["h"]) - max(float(current["o"]), float(current["c"]))
            bullish_rejection = body > 0 and lower_wick >= body * 1.25 and close_pos >= 0.65
            bearish_rejection = body > 0 and upper_wick >= body * 1.25 and close_pos <= 0.35
            sweep_side = "BUY" if lower_sweep or bullish_rejection else "SELL" if upper_sweep or bearish_rejection else None
            bull_displacement = body >= atr * 0.80 and float(current["c"]) > float(current["o"]) and close_pos >= 0.70 if atr else False
            bear_displacement = body >= atr * 0.80 and float(current["c"]) < float(current["o"]) and close_pos <= 0.30 if atr else False
            displacement_side = "BUY" if bull_displacement else "SELL" if bear_displacement else None
            micro_frame.update({
                "sweep_side": sweep_side,
                "lower_sweep": lower_sweep, "upper_sweep": upper_sweep,
                "displacement_side": displacement_side,
                "volume_confirmed": micro_frame.get("volume_confirmed", False),
                "session_ok": micro_frame.get("session_ok", False),
                "volatility_ok": micro_frame.get("volatility_ok", False),
            })

            # Manage an existing simulated campaign before looking for another entry.
            if campaign is not None:
                side = campaign["side"]
                opposite_bias = latest_alignment.get("candidate_side") not in (None, side)
                if latest_alignment.get("status") == "CONFIRMED" and opposite_bias:
                    for position in campaign["positions"]:
                        if position["status"] != "OPEN":
                            continue
                        move = (float(current["c"]) - position["entry_price"]) * (1 if side == "BUY" else -1)
                        r_now = move / position["initial_risk"] if position["initial_risk"] else 0.0
                        result_r = r_now * position["remaining_fraction"] + position["realized_r"]
                        position.update({"status": "CAMPAIGN INVALIDATED", "exit_price": float(current["c"]), "exit_time": asof, "result_r": round(result_r, 3)})
                        daily_pnl[day_key] = daily_pnl.get(day_key, 0.0) + position["risk_pct"] * result_r
                        record_closed(position)
                    campaign_entry_sizes.append(int(campaign.get("entries_count", 0)))
                    campaign["positions"] = []
                    campaign = None
                    pending = None
                    setup = None

            if campaign is not None:
                kept = []
                for position in campaign["positions"]:
                    if position["status"] != "OPEN":
                        continue
                    side = position["side"]
                    stop = float(position["stop_loss"])
                    target = float(position["take_profit"])
                    stop_hit = float(current["l"]) <= stop if side == "BUY" else float(current["h"]) >= stop
                    target_hit = float(current["h"]) >= target if side == "BUY" else float(current["l"]) <= target
                    one_r = position["entry_price"] + position["initial_risk"] * (1 if side == "BUY" else -1)
                    partial_hit = float(current["h"]) >= one_r if side == "BUY" else float(current["l"]) <= one_r

                    # Conservative OHLC sequencing: stop is counted first when SL and TP can both be hit.
                    if stop_hit:
                        result_r = position["realized_r"]
                        if position["remaining_fraction"] > 0:
                            move = (stop - position["entry_price"]) * (1 if side == "BUY" else -1)
                            result_r += (move / position["initial_risk"]) * position["remaining_fraction"]
                        position.update({"status": "STOP LOSS", "exit_price": stop, "exit_time": asof, "result_r": round(result_r, 3)})
                    else:
                        if partial_hit and not position["partial_taken"]:
                            position["realized_r"] += 0.5
                            position["remaining_fraction"] = 0.5
                            position["partial_taken"] = True
                            position["stop_loss"] = position["entry_price"]
                            position["management"] = "50% BOOKED AT 1R · STOP TO BREAKEVEN"
                        if target_hit:
                            move = (target - position["entry_price"]) * (1 if side == "BUY" else -1)
                            target_r = move / position["initial_risk"]
                            if position["remaining_fraction"] > 0:
                                position["realized_r"] += target_r * position["remaining_fraction"]
                            position["remaining_fraction"] = 0.0
                            position.update({"status": "FINAL TARGET HIT", "exit_price": target, "exit_time": asof, "result_r": round(position["realized_r"], 3)})
                        elif position["partial_taken"]:
                            # Structure-based trailing stop is only tightened, never widened.
                            swing = micro_frame.get("swing_low") if side == "BUY" else micro_frame.get("swing_high")
                            if isinstance(swing, (int, float)) and atr > 0:
                                candidate_stop = float(swing) - atr * 0.10 if side == "BUY" else float(swing) + atr * 0.10
                                if side == "BUY" and position["entry_price"] <= candidate_stop < float(current["c"]):
                                    position["stop_loss"] = max(float(position["stop_loss"]), candidate_stop)
                                elif side == "SELL" and float(current["c"]) < candidate_stop <= position["entry_price"]:
                                    position["stop_loss"] = min(float(position["stop_loss"]), candidate_stop)
                            position["management"] = "PARTIAL BOOKED · STRUCTURE TRAILING"
                    if position["status"] == "OPEN":
                        kept.append(position)
                    else:
                        daily_pnl[day_key] = daily_pnl.get(day_key, 0.0) + position["risk_pct"] * position.get("result_r", 0.0)
                        record_closed(position)
                campaign["positions"] = kept
                if not kept:
                    campaign_entry_sizes.append(int(campaign.get("entries_count", 0)))
                    campaign = None

            # Fill a signal only at the next 1m candle open, after the signal candle has closed.
            if pending is not None:
                fill_price = float(current["o"])
                side = pending["side"]
                gap = abs(fill_price - float(pending["signal_close"]))
                gap_limit = max(float(pending["atr"]) * 0.50, float(pending["signal_close"]) * 0.0002)
                if gap <= gap_limit:
                    stop = float(pending["stop_loss"])
                    risk = abs(fill_price - stop)
                    target = float(pending["take_profit"])
                    rr = ((target - fill_price) / risk) if side == "BUY" and risk > 0 else ((fill_price - target) / risk) if risk > 0 else 0.0
                    open_risk = sum(float(p["risk_pct"]) for p in campaign["positions"]) if campaign else 0.0
                    count = len(campaign["positions"]) if campaign else 0
                    can_add = (
                        campaign is None or
                        (campaign["side"] == side and count < self.MAX_POSITIONS
                         and open_risk + self.PER_POSITION_RISK_PCT <= self.MAX_CAMPAIGN_RISK_PCT)
                    )
                    if risk > 0 and rr >= self.MIN_TARGET_R and can_add and daily_pnl.get(day_key, 0.0) > -self.DAILY_MAX_LOSS_PCT:
                        if campaign is None:
                            campaign_count += 1
                            campaign = {
                                "id": f"{asset}-{asof}", "side": side, "started_at": asof,
                                "positions": [], "last_entry_price": fill_price,
                                "favorable_extreme": fill_price, "entries_count": 0,
                                "realized_r": 0.0,
                            }
                        position = {
                            "id": f"{asset}-{pending['signal_time']}",
                            "kind": pending["kind"], "side": side, "timeframe": "1m",
                            "signal_time": pending["signal_time"], "entry_time": asof,
                            "entry_price": fill_price, "planned_entry": pending["signal_close"],
                            "stop_loss": stop, "initial_stop_loss": stop,
                            "take_profit": target, "partial_take_profit": fill_price + risk * (1 if side == "BUY" else -1),
                            "initial_risk": risk, "risk_pct": self.PER_POSITION_RISK_PCT,
                            "remaining_fraction": 1.0, "partial_taken": False,
                            "realized_r": 0.0, "result_r": None, "status": "OPEN",
                            "score": pending["score"], "grade": pending["grade"],
                            "reason": pending["reason"], "management": "INITIAL SL ACTIVE",
                        }
                        campaign["positions"].append(position)
                        campaign["entries_count"] += 1
                        campaign["last_entry_price"] = fill_price
                        campaign["favorable_extreme"] = fill_price
                pending = None

            # Update campaign favourable excursion; an add-on needs a new extension first.
            if campaign is not None and campaign["positions"]:
                if campaign["side"] == "BUY":
                    campaign["favorable_extreme"] = max(float(campaign["favorable_extreme"]), float(current["h"]))
                else:
                    campaign["favorable_extreme"] = min(float(campaign["favorable_extreme"]), float(current["l"]))

            # A new 1m sequence begins only after an aligned sweep or clear rejection.
            bias_side = latest_alignment.get("side")
            if sweep_side and bias_side == sweep_side and not micro_frame.get("choppy"):
                setup = {
                    "side": sweep_side, "sweep_time": asof,
                    "sweep_price": float(current["l"] if sweep_side == "BUY" else current["h"]),
                    "start_index": index, "bos_index": None, "displacement_index": None,
                    "zone": None, "consumed": False,
                }
            if setup is not None:
                age = index - int(setup["start_index"])
                if age > 18 or setup.get("side") != bias_side or micro_frame.get("choppy"):
                    setup = None
                else:
                    side = setup["side"]
                    if setup["bos_index"] is None and micro_frame.get("bos_side") == side:
                        setup["bos_index"] = index
                    if setup["displacement_index"] is None and displacement_side == side:
                        setup["displacement_index"] = index
                    if setup["bos_index"] is not None and setup["displacement_index"] is not None:
                        if abs(int(setup["bos_index"]) - int(setup["displacement_index"])) <= 4 and setup["zone"] is None:
                            zone = micro_frame.get("fvg")
                            if not zone or zone.get("side") != side:
                                zone = micro_frame.get("order_block")
                            if zone and zone.get("side") == side:
                                setup["zone"] = zone

                    zone = setup.get("zone")
                    after_confirm = (
                        setup["bos_index"] is not None and setup["displacement_index"] is not None
                        and index > max(int(setup["bos_index"]), int(setup["displacement_index"]))
                    )
                    if zone and after_confirm and not setup.get("consumed"):
                        zlo, zhi = float(zone["low"]), float(zone["high"])
                        intersects = float(current["l"]) <= zhi and float(current["h"]) >= zlo
                        midpoint = (zlo + zhi) / 2.0
                        rejection = (
                            intersects and float(current["c"]) > midpoint and float(current["c"]) > float(current["o"])
                            if side == "BUY" else
                            intersects and float(current["c"]) < midpoint and float(current["c"]) < float(current["o"])
                        )
                        if rejection and latest_alignment.get("status") == "CONFIRMED":
                            kind = "MAIN" if campaign is None else "ADD-ON"
                            addon_ready = campaign is None
                            if campaign is not None and campaign["side"] == side:
                                excursion = (
                                    float(campaign["favorable_extreme"]) - float(campaign["last_entry_price"])
                                    if side == "BUY" else
                                    float(campaign["last_entry_price"]) - float(campaign["favorable_extreme"])
                                )
                                addon_ready = (
                                    excursion >= max(atr * 0.50, float(campaign["last_entry_price"]) * 0.0002)
                                    and len(campaign["positions"]) < self.MAX_POSITIONS
                                    and sum(float(p["risk_pct"]) for p in campaign["positions"]) + self.PER_POSITION_RISK_PCT <= self.MAX_CAMPAIGN_RISK_PCT
                                )
                            if addon_ready and daily_pnl.get(day_key, 0.0) > -self.DAILY_MAX_LOSS_PCT:
                                entry = float(current["c"])
                                stop = float(setup["sweep_price"]) - atr * 0.12 if side == "BUY" else float(setup["sweep_price"]) + atr * 0.12
                                target, target_r = self._target_for(side, entry, stop, higher)
                                price_in_discount = (
                                    micro_frame.get("premium_discount") == "DISCOUNT" if side == "BUY" else
                                    micro_frame.get("premium_discount") == "PREMIUM"
                                )
                                score_micro = {
                                    **micro_frame,
                                    "sweep_side": side,
                                    "bos_side": side if setup["bos_index"] is not None else None,
                                    "displacement_side": side if setup["displacement_index"] is not None else None,
                                }
                                score_data = self._setup_score(
                                    latest_alignment, score_micro, side, True, price_in_discount
                                )
                                rr = target_r or 0.0
                                if target is not None and rr >= self.MIN_TARGET_R and score_data["score"] >= self.MIN_SETUP_SCORE:
                                    last_signal = {
                                        "asset": asset, "timeframe": "1m", "kind": kind, "side": side,
                                        "status": "ENTRY READY · NEXT 1M OPEN",
                                        "signal_time": asof, "signal_close": entry,
                                        "entry_price": entry, "stop_loss": stop,
                                        "partial_take_profit": entry + abs(entry - stop) * (1 if side == "BUY" else -1),
                                        "take_profit": target, "target_r": round(rr, 2),
                                        "risk_pct": self.PER_POSITION_RISK_PCT,
                                        "score": score_data["score"], "grade": score_data["grade"],
                                        "score_checks": score_data["checks"],
                                        "entry_zone": zone, "sweep_price": setup["sweep_price"],
                                        "htf_alignment": latest_alignment["agreement_pct"],
                                        "reason": "HTF confirmation + 1m sweep/rejection + MSS/BOS + displacement + FVG/OB retest.",
                                        "execution": "Plan confirmed on the closed 1m candle; simulated fill is the next 1m candle open.",
                                    }
                                    pending = {
                                        "kind": kind, "side": side, "signal_time": asof,
                                        "signal_close": entry, "stop_loss": stop, "take_profit": target,
                                        "atr": atr, "score": score_data["score"], "grade": score_data["grade"],
                                        "reason": last_signal["reason"],
                                    }
                                    setup["consumed"] = True
            processed += 1

        # Final higher-timeframe snapshot is as of the latest completed 1m close.
        latest_asof = int(one[-1]["t"]) + FRAME_MS["1m"]
        final_frames: dict[str, dict[str, Any]] = {}
        for tf, rows in source_frames.items():
            count = bisect.bisect_right(close_times[tf], latest_asof) if tf != "1m" else len(one)
            available = rows[max(0, count - 320):count]
            if tf in ("1w", "1M"):
                available = [x for x in available if int(x["_close_t"]) <= latest_asof]
            else:
                available = [x for x in available if int(x["t"]) + FRAME_MS[tf] <= latest_asof]
            final_frames[tf] = self._frame_snapshot(available, tf)
        higher = {tf: final_frames.get(tf, {"ready": False, "side": None, "trend": "WARMING UP"}) for tf in HTF_ORDER}
        alignment = self._alignment(higher)
        positions = campaign["positions"] if campaign else []
        campaign_risk = round(sum(float(x["risk_pct"]) for x in positions if x["status"] == "OPEN"), 3)

        closed_count = len(closed_history)
        win_count = sum(1 for x in closed_history if float(x.get("result_r") or 0.0) > 0)
        loss_count = closed_count - win_count
        gross_win_r = sum(max(float(x.get("result_r") or 0.0), 0.0) for x in closed_history)
        gross_loss_r = sum(abs(min(float(x.get("result_r") or 0.0), 0.0)) for x in closed_history)
        cumulative_pct = peak_pct = max_drawdown_pct = 0.0
        consecutive_losses = max_consecutive_losses = 0
        holding_minutes: list[float] = []
        for item in closed_history:
            result_r = float(item.get("result_r") or 0.0)
            cumulative_pct += float(item.get("risk_pct") or 0.0) * result_r
            peak_pct = max(peak_pct, cumulative_pct)
            max_drawdown_pct = max(max_drawdown_pct, peak_pct - cumulative_pct)
            if result_r > 0:
                consecutive_losses = 0
            else:
                consecutive_losses += 1
                max_consecutive_losses = max(max_consecutive_losses, consecutive_losses)
            if item.get("entry_time") and item.get("exit_time"):
                holding_minutes.append(max(0.0, (int(item["exit_time"]) - int(item["entry_time"])) / 60_000.0))
        performance = {
            "closed_positions": closed_count,
            "wins": win_count,
            "losses": loss_count,
            "win_rate_pct": round(win_count / closed_count * 100.0, 2) if closed_count else None,
            "profit_factor": round(gross_win_r / gross_loss_r, 3) if gross_loss_r > 0 else None,
            "gross_win_r": round(gross_win_r, 3),
            "gross_loss_r": round(gross_loss_r, 3),
            "net_r": round(sum(float(x.get("result_r") or 0.0) for x in closed_history), 3),
            "max_drawdown_pct_of_equity": round(max_drawdown_pct, 3),
            "max_consecutive_losses": max_consecutive_losses,
            "average_holding_minutes": round(sum(holding_minutes) / len(holding_minutes), 1) if holding_minutes else None,
            "campaigns_started": campaign_count,
            "average_entries_per_closed_campaign": round(sum(campaign_entry_sizes) / len(campaign_entry_sizes), 2) if campaign_entry_sizes else None,
            "sample_scope": "Sequential replay of the loaded closed 1m candles; capped by AST_CAMPAIGN_REPLAY_BARS.",
            "fees_slippage_included": False,
        }
        can_add = bool(
            campaign and len(positions) < self.MAX_POSITIONS
            and campaign_risk + self.PER_POSITION_RISK_PCT <= self.MAX_CAMPAIGN_RISK_PCT
        )
        if pending is not None and last_signal is not None:
            execution = dict(last_signal)
        elif positions:
            execution = {
                "asset": asset, "timeframe": "1m", "status": "CAMPAIGN ACTIVE",
                "side": campaign["side"], "kind": "MAIN + ADD-ONS",
                "entry_price": positions[-1]["entry_price"], "stop_loss": positions[-1]["stop_loss"],
                "partial_take_profit": positions[-1]["partial_take_profit"],
                "take_profit": positions[-1]["take_profit"],
                "open_positions": len(positions), "campaign_risk_pct": campaign_risk,
                "reason": "Open simulated 1m campaign; each position retains its own stop and final target.",
            }
        else:
            execution = None

        if execution:
            status = execution["status"]
            reason = execution.get("reason", "")
        elif alignment["status"] != "CONFIRMED":
            status = "WAITING FOR HTF CONFIRMATION"
            reason = alignment["reason"]
        elif latest_micro.get("choppy"):
            status = "NO TRADE · CHOPPY MARKET"
            reason = "The current 1m market is compressed/choppy; wait for directional efficiency and a fresh confirmed sequence."
        else:
            status = "WAITING FOR 1M PULLBACK"
            reason = "HTF bias confirmed; waiting for a fresh 1m sweep/rejection, MSS/BOS, displacement and FVG/OB retest."

        return {
            "asset": asset,
            "strategy_name": "AST Multi-Timeframe Institutional Trap & Continuous Pyramiding",
            "status": status, "reason": reason,
            "higher_timeframes": higher,
            "alignment": alignment,
            "execution_1m": execution,
            "campaign": {
                "status": "ACTIVE" if campaign else "NO CAMPAIGN",
                "id": campaign.get("id") if campaign else None,
                "side": campaign.get("side") if campaign else None,
                "started_at": campaign.get("started_at") if campaign else None,
                "positions": positions,
                "open_positions": len(positions),
                "entries_count": campaign.get("entries_count", 0) if campaign else 0,
                "risk_pct": campaign_risk,
                "max_risk_pct": self.MAX_CAMPAIGN_RISK_PCT,
                "per_position_risk_pct": self.PER_POSITION_RISK_PCT,
                "max_positions": self.MAX_POSITIONS,
                "can_add_position": can_add,
                "daily_loss_limit_pct": self.DAILY_MAX_LOSS_PCT,
            },
            "recent_closed": recent_closed[-10:],
            "performance": performance,
            "replay": {
                "bars_processed": processed,
                "bars_available": len(one),
                "first_bar_time": int(one[0]["t"]),
                "last_bar_close_time": latest_asof,
                "lookahead_safe": True,
                "execution_model": "Signal on closed 1m candle; fill at next 1m open; if SL and target touch in one candle, SL is counted first.",
            },
            "data_notes": [
                "1M and 1w candles are calendar aggregates of completed daily candles and are not exposed before their period closes.",
                "SMT divergence, order-flow absorption, volume delta, volume profile and news risk are not scored unless a verified source is connected; they are not fabricated.",
                "Risk percentages are planning allocations; live quantity requires account equity, contract size and broker-specific tick values.",
                "Signals are analytical plans and simulated replay entries, not broker orders.",
                "Backtest performance excludes commissions and slippage; use separate out-of-sample and forward tests before live use.",
            ],
        }
