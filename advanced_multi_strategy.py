from __future__ import annotations

from dataclasses import dataclass
from typing import Any

TIMEFRAME_ORDER = ("1m", "5m", "15m", "30m", "1h", "4h", "1d")
ENGINE_NAMES = ("trend", "liquidity", "breakout", "range", "momentum", "price_action")
MIN_BARS = 80
BACKTEST_LIMIT = 25_000
TARGET_R = 2.0


def _finite(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
        return result if result == result and abs(result) != float("inf") else default
    except (TypeError, ValueError):
        return default


def _ema(values: list[float], period: int) -> float:
    if not values:
        return 0.0
    alpha = 2.0 / (period + 1.0)
    value = values[0]
    for item in values[1:]:
        value = alpha * item + (1.0 - alpha) * value
    return value


def _rsi(closes: list[float], period: int = 14) -> float | None:
    if len(closes) < period + 1:
        return None
    gains = 0.0
    losses = 0.0
    for i in range(len(closes) - period, len(closes)):
        change = closes[i] - closes[i - 1]
        if change > 0:
            gains += change
        elif change < 0:
            losses -= change
    if losses == 0:
        return 100.0 if gains else 50.0
    rs = (gains / period) / (losses / period)
    return 100.0 - (100.0 / (1.0 + rs))


def _atr(highs: list[float], lows: list[float], closes: list[float], period: int = 14) -> float:
    if len(closes) < 2:
        return 0.0
    start = max(1, len(closes) - period)
    true_ranges = []
    for i in range(start, len(closes)):
        true_ranges.append(max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        ))
    return sum(true_ranges) / len(true_ranges) if true_ranges else 0.0


def _confirmed_pivots(
    highs: list[float], lows: list[float], index: int, left: int = 2, right: int = 2,
    lookback: int = 120,
) -> tuple[list[tuple[int, float]], list[tuple[int, float]]]:
    """Return pivots whose right-hand confirmation candles have already closed."""
    pivot_highs: list[tuple[int, float]] = []
    pivot_lows: list[tuple[int, float]] = []
    start = max(left, index - lookback)
    end = index - right
    for j in range(start, end + 1):
        high_window = highs[j - left:j + right + 1]
        low_window = lows[j - left:j + right + 1]
        if len(high_window) == left + right + 1 and highs[j] == max(high_window):
            if highs[j] > highs[j - 1] and highs[j] >= highs[j + 1]:
                pivot_highs.append((j, highs[j]))
        if len(low_window) == left + right + 1 and lows[j] == min(low_window):
            if lows[j] < lows[j - 1] and lows[j] <= lows[j + 1]:
                pivot_lows.append((j, lows[j]))
    return pivot_highs, pivot_lows


def _body_metrics(candle: dict[str, Any]) -> tuple[float, float, float, float]:
    op = _finite(candle.get("o"))
    high = _finite(candle.get("h"))
    low = _finite(candle.get("l"))
    close = _finite(candle.get("c"))
    span = max(high - low, 1e-12)
    body = abs(close - op)
    close_position = (close - low) / span
    upper_wick = high - max(op, close)
    lower_wick = min(op, close) - low
    return body, close_position, upper_wick, lower_wick


@dataclass
class Evaluation:
    side: str | None
    status: str
    reason: str
    engines: dict[str, dict[str, Any]]
    stop_anchor: float | None
    risk_unit: float | None
    candle_time: int | None
    features: dict[str, Any]


class AdvancedMultiStrategyEngine:
    """One point-in-time signal evaluator shared by live decisions and historical replay."""

    def __init__(self, asset: str = "MARKET") -> None:
        self.asset = asset.upper()
        self._backtest_cache: dict[str, dict[str, Any]] = {}
        self._backtest_signatures: dict[str, tuple[Any, ...]] = {}

    @staticmethod
    def _validate(candles: list[dict[str, Any]]) -> list[dict[str, float | int]]:
        clean: list[dict[str, float | int]] = []
        last_t = -1
        for row in candles:
            try:
                t = int(row.get("t", 0))
                o, h, l, c = (_finite(row.get(k), float("nan")) for k in ("o", "h", "l", "c"))
                v = max(0.0, _finite(row.get("v")))
                if t <= last_t or any(x != x or abs(x) == float("inf") for x in (o, h, l, c)):
                    continue
                if h < max(o, c, l) or l > min(o, c, h) or l <= 0:
                    continue
                clean.append({"t": t, "o": o, "h": h, "l": l, "c": c, "v": v})
                last_t = t
            except (TypeError, ValueError):
                continue
        return clean[-BACKTEST_LIMIT:]

    def evaluate(self, candles: list[dict[str, Any]], index: int | None = None) -> Evaluation:
        return self._evaluate_clean(self._validate(candles), index)

    def _evaluate_clean(self, data: list[dict[str, Any]], index: int | None = None) -> Evaluation:
        i = len(data) - 1 if index is None else min(max(int(index), 0), len(data) - 1)
        if len(data) < MIN_BARS or i < MIN_BARS - 1:
            return Evaluation(None, "WARMING UP", f"Need at least {MIN_BARS} completed candles; received {len(data)}.", {}, None, None, None, {})
        # Fixed lookback keeps replay bounded. No candle after i can enter this view.
        view_start = max(0, i - 260)
        view = data[view_start:i + 1]
        i = len(view) - 1
        opens = [float(x["o"]) for x in view]
        highs = [float(x["h"]) for x in view]
        lows = [float(x["l"]) for x in view]
        closes = [float(x["c"]) for x in view]
        volumes = [float(x["v"]) for x in view]
        current = view[-1]
        previous = view[-2]
        close = closes[-1]
        atr = _atr(highs, lows, closes, 14)
        ema20 = _ema(closes[-min(len(closes), 120):], 20)
        ema50 = _ema(closes[-min(len(closes), 240):], 50)
        ema20_prev = _ema(closes[-min(len(closes), 125):-5], 20) if len(closes) >= 25 else ema20
        rsi = _rsi(closes, 14)
        body, close_pos, upper_wick, lower_wick = _body_metrics(current)
        prev_body = abs(float(previous["c"]) - float(previous["o"]))
        bull_candle = close > float(current["o"])
        bear_candle = close < float(current["o"])
        bull_displacement = bool(atr and bull_candle and body >= 0.60 * atr and close_pos >= 0.70)
        bear_displacement = bool(atr and bear_candle and body >= 0.60 * atr and close_pos <= 0.30)
        pivot_highs, pivot_lows = _confirmed_pivots(highs, lows, i)
        last_two_highs = [x[1] for x in pivot_highs[-2:]]
        last_two_lows = [x[1] for x in pivot_lows[-2:]]
        structure_up = len(last_two_highs) == 2 and len(last_two_lows) == 2 and last_two_highs[-1] > last_two_highs[-2] and last_two_lows[-1] > last_two_lows[-2]
        structure_down = len(last_two_highs) == 2 and len(last_two_lows) == 2 and last_two_highs[-1] < last_two_highs[-2] and last_two_lows[-1] < last_two_lows[-2]
        ema_up = close > ema20 > ema50 and ema20 >= ema20_prev
        ema_down = close < ema20 < ema50 and ema20 <= ema20_prev
        trend_side = "BUY" if structure_up and ema_up else "SELL" if structure_down and ema_down else None

        range_start = max(0, i - 20)
        prior_high = max(highs[range_start:i])
        prior_low = min(lows[range_start:i])
        range_width = prior_high - prior_low
        range_atr = range_width / atr if atr else None
        vol_window = volumes[max(0, i - 20):i]
        avg_volume = sum(vol_window) / len(vol_window) if vol_window else 0.0
        volume_ratio = volumes[-1] / avg_volume if avg_volume > 0 else None

        # 1) TREND ENGINE: confirmed HH/HL or LH/LL must agree with EMA structure/slope.
        trend_direction = "BULLISH" if trend_side == "BUY" else "BEARISH" if trend_side == "SELL" else "NEUTRAL"
        trend_reason = "Confirmed HH/HL + price above rising EMA20/EMA50" if trend_side == "BUY" else "Confirmed LH/LL + price below falling EMA20/EMA50" if trend_side == "SELL" else "No aligned confirmed swing sequence and EMA trend"

        # 2) LIQUIDITY ENGINE: equal pools and sweep -> later close through the post-sweep structure.
        tolerance = atr * 0.20 if atr else 0.0
        equal_high = len(pivot_highs) >= 2 and abs(pivot_highs[-1][1] - pivot_highs[-2][1]) <= tolerance
        equal_low = len(pivot_lows) >= 2 and abs(pivot_lows[-1][1] - pivot_lows[-2][1]) <= tolerance
        buy_liquidity = pivot_highs[-1][1] if pivot_highs else prior_high
        sell_liquidity = pivot_lows[-1][1] if pivot_lows else prior_low
        current_upper_sweep = highs[-1] > prior_high and close < prior_high
        current_lower_sweep = lows[-1] < prior_low and close > prior_low
        confirmed_sweep_side: str | None = None
        sweep_source = "No confirmed sweep"
        search_from = max(20, i - 5)
        for j in range(i - 1, search_from - 1, -1):
            old_hi = max(highs[max(0, j - 20):j])
            old_lo = min(lows[max(0, j - 20):j])
            if lows[j] < old_lo and closes[j] > old_lo and close > max(highs[j:i]):
                confirmed_sweep_side = "BUY"
                sweep_source = "Sell-side sweep followed by bullish structure reclaim"
                break
            if highs[j] > old_hi and closes[j] < old_hi and close < min(lows[j:i]):
                confirmed_sweep_side = "SELL"
                sweep_source = "Buy-side sweep followed by bearish structure reclaim"
                break
        liquidity_side = confirmed_sweep_side
        liquidity_direction = "BULLISH" if liquidity_side == "BUY" else "BEARISH" if liquidity_side == "SELL" else "NEUTRAL"
        liquidity_reason = sweep_source
        if liquidity_side is None and (current_lower_sweep or current_upper_sweep):
            liquidity_reason = "Sweep detected; waiting for post-sweep structure confirmation"

        # 3) BREAKOUT ENGINE: close beyond a prior range with displacement and available volume/momentum support.
        vol_ok = volume_ratio is None or volume_ratio >= 1.10
        breakout_up = bool(close > prior_high and bull_displacement and vol_ok)
        breakout_down = bool(close < prior_low and bear_displacement and vol_ok)
        breakout_level = prior_high if breakout_up else prior_low if breakout_down else None
        if not breakout_up and not breakout_down:
            # Confirm a retest only after the original breakout candle has closed.
            for j in range(max(20, i - 5), i):
                old_hi = max(highs[max(0, j - 20):j])
                old_lo = min(lows[max(0, j - 20):j])
                old_body, old_pos, _, _ = _body_metrics(view[j])
                old_atr = _atr(highs[:j + 1], lows[:j + 1], closes[:j + 1], 14)
                if closes[j] > old_hi and old_body >= 0.60 * old_atr and old_pos >= 0.70 and lows[-1] <= old_hi + 0.20 * max(atr, 1e-12) and close > old_hi and bull_candle:
                    breakout_up, breakout_level = True, old_hi
                    break
                if closes[j] < old_lo and old_body >= 0.60 * old_atr and old_pos <= 0.30 and highs[-1] >= old_lo - 0.20 * max(atr, 1e-12) and close < old_lo and bear_candle:
                    breakout_down, breakout_level = True, old_lo
                    break
        breakout_side = "BUY" if breakout_up else "SELL" if breakout_down else None
        breakout_direction = "BULLISH" if breakout_up else "BEARISH" if breakout_down else "NEUTRAL"
        breakout_reason = "Strong close beyond prior range with displacement/volume support" if breakout_side else "No confirmed close-and-retest breakout"

        # 4) RANGE ENGINE: only trade edges of a compressed range after rejection; never its middle.
        is_range = bool(atr and range_atr is not None and range_atr <= 5.5)
        lower_edge = close <= prior_low + range_width * 0.22
        upper_edge = close >= prior_high - range_width * 0.22
        range_buy = is_range and lower_edge and bull_candle and (lower_wick >= max(body * 1.25, atr * 0.10)) and close_pos >= 0.60 and not breakout_down
        range_sell = is_range and upper_edge and bear_candle and (upper_wick >= max(body * 1.25, atr * 0.10)) and close_pos <= 0.40 and not breakout_up
        range_side = "BUY" if range_buy else "SELL" if range_sell else None
        range_direction = "BULLISH" if range_buy else "BEARISH" if range_sell else "NEUTRAL"
        range_reason = "Compressed range lower-edge rejection" if range_buy else "Compressed range upper-edge rejection" if range_sell else "No confirmed range-edge rejection; middle entries blocked" if is_range else "Range expansion / breakout conditions"

        # 5) MOMENTUM ENGINE: RSI, EMA slope, candle strength, divergence and exhaustion.
        rsi_value = rsi if rsi is not None else 50.0
        exhaustion_up = rsi_value >= 76 and body < 0.45 * max(atr, 1e-12)
        exhaustion_down = rsi_value <= 24 and body < 0.45 * max(atr, 1e-12)
        bullish_divergence = False
        bearish_divergence = False
        if len(pivot_lows) >= 2:
            (low_idx_a, low_a), (low_idx_b, low_b) = pivot_lows[-2], pivot_lows[-1]
            rsi_a = _rsi(closes[:low_idx_a + 1], 14)
            rsi_b = _rsi(closes[:low_idx_b + 1], 14)
            bullish_divergence = bool(low_b < low_a and rsi_a is not None and rsi_b is not None and rsi_b > rsi_a + 2.0)
        if len(pivot_highs) >= 2:
            (high_idx_a, high_a), (high_idx_b, high_b) = pivot_highs[-2], pivot_highs[-1]
            rsi_a = _rsi(closes[:high_idx_a + 1], 14)
            rsi_b = _rsi(closes[:high_idx_b + 1], 14)
            bearish_divergence = bool(high_b > high_a and rsi_a is not None and rsi_b is not None and rsi_b < rsi_a - 2.0)
        momentum_buy = close > ema20 and ema20 >= ema20_prev and (rsi_value >= 54 or bullish_divergence) and body >= 0.25 * max(atr, 1e-12) and not exhaustion_up
        momentum_sell = close < ema20 and ema20 <= ema20_prev and (rsi_value <= 46 or bearish_divergence) and body >= 0.25 * max(atr, 1e-12) and not exhaustion_down
        momentum_side = "BUY" if momentum_buy else "SELL" if momentum_sell else None
        momentum_direction = "BULLISH" if momentum_buy else "BEARISH" if momentum_sell else "NEUTRAL"
        momentum_reason = (f"RSI {rsi_value:.1f}, EMA20 slope, candle strength" + (", bullish divergence" if bullish_divergence else ", bearish divergence" if bearish_divergence else "") + " aligned") if momentum_side else "Momentum weak, mixed or exhausted"

        # 6) PRICE ACTION ENGINE: BOS/CHOCH plus candle pattern, FVG or order-block reaction.
        last_pivot_high = pivot_highs[-1][1] if pivot_highs else None
        last_pivot_low = pivot_lows[-1][1] if pivot_lows else None
        bos_up = last_pivot_high is not None and close > last_pivot_high and bull_displacement
        bos_down = last_pivot_low is not None and close < last_pivot_low and bear_displacement
        choch_up = trend_direction == "BEARISH" and bos_up
        choch_down = trend_direction == "BULLISH" and bos_down
        prev_open, prev_close = float(previous["o"]), float(previous["c"])
        bullish_engulf = bull_candle and prev_close < prev_open and float(current["o"]) <= prev_close and close >= prev_open and body >= prev_body
        bearish_engulf = bear_candle and prev_close > prev_open and float(current["o"]) >= prev_close and close <= prev_open and body >= prev_body
        bullish_rejection = bull_candle and lower_wick >= max(body * 1.5, atr * 0.15) and close_pos >= 0.68
        bearish_rejection = bear_candle and upper_wick >= max(body * 1.5, atr * 0.15) and close_pos <= 0.32
        bullish_fvg = i >= 2 and lows[-1] > highs[-3]
        bearish_fvg = i >= 2 and highs[-1] < lows[-3]
        bull_ob_reaction = False
        bear_ob_reaction = False
        for j in range(max(0, i - 6), i):
            prior = view[j]
            if float(prior["c"]) < float(prior["o"]) and lows[-1] <= float(prior["h"]) and close > float(prior["o"]):
                bull_ob_reaction = True
            if float(prior["c"]) > float(prior["o"]) and highs[-1] >= float(prior["l"]) and close < float(prior["o"]):
                bear_ob_reaction = True
        bull_pa_pattern = bullish_engulf or bullish_rejection or bullish_fvg or bull_ob_reaction
        bear_pa_pattern = bearish_engulf or bearish_rejection or bearish_fvg or bear_ob_reaction
        pa_buy = bool((bos_up or choch_up) and bull_pa_pattern)
        pa_sell = bool((bos_down or choch_down) and bear_pa_pattern)
        pa_side = "BUY" if pa_buy else "SELL" if pa_sell else None
        pa_direction = "BULLISH" if pa_buy else "BEARISH" if pa_sell else "NEUTRAL"
        pa_sources = [name for name, hit in (("BOS", bos_up or bos_down), ("CHOCH", choch_up or choch_down), ("ENGULFING", bullish_engulf or bearish_engulf), ("REJECTION", bullish_rejection or bearish_rejection), ("FVG", bullish_fvg or bearish_fvg), ("ORDER_BLOCK_REACTION", bull_ob_reaction or bear_ob_reaction)) if hit]
        pa_reason = " / ".join(pa_sources) if pa_side else "Waiting for BOS/CHOCH plus a confirmed candle/FVG/order-block reaction"

        engines = {
            "trend": {"direction": trend_direction, "side": trend_side, "reason": trend_reason, "hh_hl": bool(structure_up), "lh_ll": bool(structure_down), "ema20": ema20, "ema50": ema50},
            "liquidity": {"direction": liquidity_direction, "side": liquidity_side, "reason": liquidity_reason, "equal_high": bool(equal_high), "equal_low": bool(equal_low), "buy_side_liquidity": buy_liquidity, "sell_side_liquidity": sell_liquidity, "upper_sweep_pending": bool(current_upper_sweep), "lower_sweep_pending": bool(current_lower_sweep)},
            "breakout": {"direction": breakout_direction, "side": breakout_side, "reason": breakout_reason, "level": breakout_level, "volume_ratio": volume_ratio},
            "range": {"direction": range_direction, "side": range_side, "reason": range_reason, "active": is_range, "range_high": prior_high, "range_low": prior_low, "range_width_atr": range_atr},
            "momentum": {"direction": momentum_direction, "side": momentum_side, "reason": momentum_reason, "rsi14": rsi, "bullish_divergence": bool(bullish_divergence), "bearish_divergence": bool(bearish_divergence), "exhaustion": bool(exhaustion_up or exhaustion_down)},
            "price_action": {"direction": pa_direction, "side": pa_side, "reason": pa_reason, "bos_bullish": bool(bos_up), "bos_bearish": bool(bos_down), "choch_bullish": bool(choch_up), "choch_bearish": bool(choch_down), "sources": pa_sources},
        }
        votes = {name: info.get("side") for name, info in engines.items()}
        buy_votes = sum(1 for value in votes.values() if value == "BUY")
        sell_votes = sum(1 for value in votes.values() if value == "SELL")
        trigger_buy = any(votes[name] == "BUY" for name in ("liquidity", "breakout", "range"))
        trigger_sell = any(votes[name] == "SELL" for name in ("liquidity", "breakout", "range"))
        buy_ready = buy_votes >= 4 and votes["price_action"] == "BUY" and trigger_buy and (votes["trend"] == "BUY" or votes["momentum"] == "BUY")
        sell_ready = sell_votes >= 4 and votes["price_action"] == "SELL" and trigger_sell and (votes["trend"] == "SELL" or votes["momentum"] == "SELL")
        side = "BUY" if buy_ready and not sell_ready else "SELL" if sell_ready and not buy_ready else None
        reason = f"{buy_votes}/6 bullish engine confirmations" if side == "BUY" else f"{sell_votes}/6 bearish engine confirmations" if side == "SELL" else f"WAITING · BUY {buy_votes}/6, SELL {sell_votes}/6; require 4 aligned engines including Price Action and a trigger engine"

        stop_anchor: float | None = None
        if side and atr > 0:
            buffer = max(0.15 * atr, close * 0.00005)
            if side == "BUY":
                structural_lows = [price for _, price in pivot_lows[-3:]]
                anchor = min([float(current["l"])] + structural_lows) if structural_lows else float(current["l"])
                stop_anchor = anchor - buffer
            else:
                structural_highs = [price for _, price in pivot_highs[-3:]]
                anchor = max([float(current["h"])] + structural_highs) if structural_highs else float(current["h"])
                stop_anchor = anchor + buffer
            estimated_risk = close - stop_anchor if side == "BUY" else stop_anchor - close
            if estimated_risk <= 0 or estimated_risk > atr * 6.0:
                side = None
                stop_anchor = None
                reason = "WAITING · Structure-based stop is invalid or wider than 6 ATR"

        return Evaluation(
            side=side,
            status="SIGNAL READY" if side else "WAITING",
            reason=reason,
            engines=engines,
            stop_anchor=stop_anchor,
            risk_unit=atr,
            candle_time=int(current["t"]),
            features={"atr14": atr, "ema20": ema20, "ema50": ema50, "rsi14": rsi, "volume_ratio": volume_ratio, "range_high": prior_high, "range_low": prior_low, "range_width_atr": range_atr, "buy_votes": buy_votes, "sell_votes": sell_votes},
        )

    @staticmethod
    def _plan(side: str, entry: float, stop: float, signal_time: int, tf: str) -> dict[str, Any] | None:
        risk = entry - stop if side == "BUY" else stop - entry
        if not all(map(lambda x: x > 0, (entry, risk))) or risk > entry * 0.10:
            return None
        target = entry + TARGET_R * risk if side == "BUY" else entry - TARGET_R * risk
        return {
            "side": side,
            "entry_price": round(entry, 8),
            "stop_loss": round(stop, 8),
            "take_profit": round(target, 8),
            "risk_reward": TARGET_R,
            "risk_distance": round(risk, 8),
            "signal_time": signal_time,
            "timeframe": tf,
            "execution": "Next candle open in backtest; current live reference for live plan",
        }

    def live_timeframe(self, candles: list[dict[str, Any]], timeframe: str, live_price: float | None = None) -> dict[str, Any]:
        data = self._validate(candles)
        evaluation = self._evaluate_clean(data)
        result: dict[str, Any] = {
            "timeframe": timeframe,
            "status": evaluation.status,
            "side": evaluation.side,
            "reason": evaluation.reason,
            "candle_time": evaluation.candle_time,
            "engines": evaluation.engines,
            "features": evaluation.features,
            "trade_plan": None,
        }
        if evaluation.side and evaluation.stop_anchor is not None and evaluation.candle_time is not None:
            entry = _finite(live_price, _finite(data[-1]["c"])) if data else 0.0
            plan = self._plan(evaluation.side, entry, evaluation.stop_anchor, evaluation.candle_time, timeframe)
            if plan:
                invalidated = (evaluation.side == "BUY" and entry <= evaluation.stop_anchor) or (evaluation.side == "SELL" and entry >= evaluation.stop_anchor)
                if invalidated:
                    result["status"] = "CANCELLED"
                    result["side"] = None
                    result["reason"] = "Setup invalidated: live price has crossed the structure stop before entry"
                else:
                    result["trade_plan"] = plan
            else:
                result["status"] = "WAITING"
                result["side"] = None
                result["reason"] = "Risk plan invalid at the current live reference price"
        return result

    @staticmethod
    def _simulate_trade(
        candles: list[dict[str, Any]], entry_index: int, side: str, entry: float, stop: float,
        signal_time: int, timeframe: str, asset: str,
    ) -> dict[str, Any] | None:
        risk = entry - stop if side == "BUY" else stop - entry
        if risk <= 0 or entry <= 0:
            return None
        target = entry + TARGET_R * risk if side == "BUY" else entry - TARGET_R * risk
        for j in range(entry_index, len(candles)):
            bar = candles[j]
            op, high, low = (_finite(bar.get(k)) for k in ("o", "h", "l"))
            exit_price: float | None = None
            status: str | None = None
            stop_hit = False
            target_hit = False
            if j > entry_index:
                if side == "BUY" and op <= stop:
                    exit_price, status = op, "LOSS"
                elif side == "SELL" and op >= stop:
                    exit_price, status = op, "LOSS"
                elif side == "BUY" and op >= target:
                    exit_price, status = op, "WIN"
                elif side == "SELL" and op <= target:
                    exit_price, status = op, "WIN"
            if exit_price is None:
                stop_hit = low <= stop if side == "BUY" else high >= stop
                target_hit = high >= target if side == "BUY" else low <= target
                if stop_hit and target_hit:
                    # Conservative intrabar ordering: assume SL was touched first.
                    exit_price, status = stop, "LOSS"
                elif stop_hit:
                    exit_price, status = stop, "LOSS"
                elif target_hit:
                    exit_price, status = target, "WIN"
            if exit_price is not None and status:
                result_r = (exit_price - entry) / risk if side == "BUY" else (entry - exit_price) / risk
                return {
                    "trade_id": f"{asset}:{timeframe}:{signal_time}:{side}",
                    "asset": asset,
                    "timeframe": timeframe,
                    "side": side,
                    "signal_time": signal_time,
                    "entry_time": int(bar.get("t", 0)),
                    "exit_time": int(bar.get("t", 0)),
                    "entry_price": round(entry, 8),
                    "stop_loss": round(stop, 8),
                    "take_profit": round(target, 8),
                    "exit_price": round(exit_price, 8),
                    "result": status,
                    "result_r": round(result_r, 4),
                    "target_r": TARGET_R,
                    "exit_reason": "SL_FIRST_SAME_CANDLE" if stop_hit and target_hit else ("STOP_LOSS" if status == "LOSS" else "TAKE_PROFIT"),
                }
        return None

    def backtest(self, candles: list[dict[str, Any]], timeframe: str) -> dict[str, Any]:
        data = self._validate(candles)
        if len(data) < MIN_BARS + 2:
            return self._empty_backtest(timeframe, len(data), "Not enough completed historical candles")
        trades: list[dict[str, Any]] = []
        next_available_index = MIN_BARS - 1
        unclosed = 0
        for i in range(MIN_BARS - 1, len(data) - 1):
            if i < next_available_index:
                continue
            evaluation = self._evaluate_clean(data, i)
            if not evaluation.side or evaluation.stop_anchor is None:
                continue
            entry_index = i + 1
            entry = float(data[entry_index]["o"])
            trade = self._simulate_trade(
                data, entry_index, evaluation.side, entry, evaluation.stop_anchor,
                int(data[i]["t"]), timeframe, self.asset,
            )
            if trade is None:
                unclosed += 1
                next_available_index = len(data)
                continue
            trades.append(trade)
            next_available_index = next((j for j in range(entry_index + 1, len(data)) if int(data[j]["t"]) == trade["exit_time"]), entry_index) + 1

        results = [float(x["result_r"]) for x in trades]
        wins = [x for x in trades if x["result"] == "WIN"]
        losses = [x for x in trades if x["result"] == "LOSS"]
        winning_r = sum(max(0.0, x) for x in results)
        losing_r = sum(abs(min(0.0, x)) for x in results)
        net_r = winning_r - losing_r
        equity = peak = 0.0
        max_dd = 0.0
        for value in results:
            equity += value
            peak = max(peak, equity)
            max_dd = max(max_dd, peak - equity)
        max_win_streak = max_loss_streak = win_streak = loss_streak = 0
        for trade in trades:
            if trade["result"] == "WIN":
                win_streak += 1
                loss_streak = 0
            else:
                loss_streak += 1
                win_streak = 0
            max_win_streak = max(max_win_streak, win_streak)
            max_loss_streak = max(max_loss_streak, loss_streak)
        avg_win = sum(float(x["result_r"]) for x in wins) / len(wins) if wins else None
        avg_loss = losing_r / len(losses) if losses else None
        closed = len(trades)
        return {
            "timeframe": timeframe,
            "status": "CALCULATED",
            "bars_available": len(data),
            "bars_tested": len(data),
            "first_candle_time": int(data[0]["t"]),
            "last_candle_time": int(data[-1]["t"]),
            "total_historical_trades": closed,
            "total_winning_trades": len(wins),
            "total_losing_trades": len(losses),
            "open_unclosed_setups": unclosed,
            "win_rate_pct": round(len(wins) / closed * 100.0, 2) if closed else None,
            "loss_rate_pct": round(len(losses) / closed * 100.0, 2) if closed else None,
            "actual_risk_reward_ratio": round(avg_win / avg_loss, 4) if avg_win is not None and avg_loss else None,
            "target_r": TARGET_R,
            "total_winning_r": round(winning_r, 4),
            "total_losing_r": round(losing_r, 4),
            "net_profit_loss_r": round(net_r, 4),
            "maximum_drawdown_r": round(max_dd, 4),
            "maximum_consecutive_wins": max_win_streak,
            "maximum_consecutive_losses": max_loss_streak,
            "profit_factor": round(winning_r / losing_r, 4) if losing_r else (None if not winning_r else "INF"),
            "average_winning_r": round(avg_win, 4) if avg_win is not None else None,
            "average_losing_r": round(avg_loss, 4) if avg_loss is not None else None,
            "costs": {"fees": "NOT INCLUDED", "spread": "NOT INCLUDED", "slippage": "NOT INCLUDED", "reason": "Historical bid/ask and account-specific fee data are not available in the candle dataset."},
            "assumptions": ["Signal uses only completed candles up to its timestamp.", "Entry is the next candle open.", "Minimum target is 2R from the actual simulated entry and structure stop.", "If SL and TP are both inside one candle and lower-timeframe data is unavailable, SL is counted first.", "No future candles or lookahead values are used."],
            "trades": trades,
        }

    @staticmethod
    def _empty_backtest(timeframe: str, bars: int, reason: str) -> dict[str, Any]:
        return {
            "timeframe": timeframe, "status": "WARMING UP", "bars_available": bars,
            "bars_tested": 0, "total_historical_trades": 0, "total_winning_trades": 0,
            "total_losing_trades": 0, "win_rate_pct": None, "loss_rate_pct": None,
            "actual_risk_reward_ratio": None, "total_winning_r": 0.0,
            "total_losing_r": 0.0, "net_profit_loss_r": 0.0,
            "maximum_drawdown_r": 0.0, "maximum_consecutive_wins": 0,
            "maximum_consecutive_losses": 0, "profit_factor": None,
            "trades": [], "reason": reason,
        }

    def snapshot(self, candles_by_tf: dict[str, Any], live_price: float | None = None) -> dict[str, Any]:
        timeframes: dict[str, Any] = {}
        performance: dict[str, Any] = {}
        for tf in TIMEFRAME_ORDER:
            raw = list(candles_by_tf.get(tf, []))
            clean = self._validate(raw)
            if clean:
                sig = (len(clean), int(clean[0]["t"]), int(clean[-1]["t"]))
            else:
                sig = (0, None, None)
            if self._backtest_signatures.get(tf) != sig:
                self._backtest_cache[tf] = self.backtest(clean, tf)
                self._backtest_signatures[tf] = sig
            performance[tf] = {k: v for k, v in self._backtest_cache.get(tf, self._empty_backtest(tf, len(clean), "Backtest queued")).items() if k != "trades"}
            timeframes[tf] = self.live_timeframe(clean, tf, live_price)
        one_minute = timeframes.get("1m", {})
        live_status = one_minute.get("status", "WARMING UP")
        return {
            "asset": self.asset,
            "strategy_name": "Advanced Multi-Strategy Trading System",
            "live_reference_price": live_price,
            "status": live_status,
            "reason": one_minute.get("reason", "Waiting for completed candles"),
            "timeframe_results": timeframes,
            "historical_performance": performance,
            "backtest_scope": {"timeframes": list(TIMEFRAME_ORDER), "maximum_bars_per_timeframe": BACKTEST_LIMIT, "same_logic_as_live": True, "results_saved_in_process_cache": True},
        }

    def historical_trades(self, timeframe: str) -> dict[str, Any]:
        return self._backtest_cache.get(timeframe, self._empty_backtest(timeframe, 0, "Backtest not calculated yet"))
