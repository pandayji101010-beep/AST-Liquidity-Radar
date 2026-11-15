from __future__ import annotations

import bisect
import os
from collections import defaultdict
from typing import Any

TIMEFRAME_ORDER = ("1m", "5m", "15m", "30m", "1h", "4h", "1d")
TIMEFRAME_MS = {
    "1m": 60_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000,
    "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000,
}
BACKTEST_LIMIT = 25_000
TARGET_R = 2.0
MIN_BARS = 50
MIN_REPORT_TRADES = 30
MIN_SPLIT_TRADES = 10

COMBINATIONS: dict[str, dict[str, Any]] = {
    "A": {
        "label": "4H → 1H → 30M → 15M → 5M → 1M",
        "higher": ("4h", "1h"),
        "middle": ("30m", "15m"),
        "pullback": "5m",
        "entry": "1m",
    },
    "B": {
        "label": "1H → 30M → 15M → 5M → 1M",
        "higher": ("1h",),
        "middle": ("30m", "15m"),
        "pullback": "5m",
        "entry": "1m",
    },
    "C": {
        "label": "15M → 5M → 1M",
        "higher": ("15m",),
        "middle": ("15m",),
        "pullback": "5m",
        "entry": "1m",
    },
}


def _number(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
        return result if result == result and abs(result) != float("inf") else default
    except (TypeError, ValueError, OverflowError):
        return default


def _ema(values: list[float], period: int) -> float:
    if not values:
        return 0.0
    alpha = 2.0 / (period + 1.0)
    result = values[0]
    for value in values[1:]:
        result = alpha * value + (1.0 - alpha) * result
    return result


def _ema_pair(candles: list[dict[str, Any]]) -> tuple[float, float, float, float]:
    closes = [float(x["c"]) for x in candles]
    current = closes[-120:]
    prior = closes[-123:-3]
    if not prior:
        prior = current[:-1] or current
    return _ema(current, 9), _ema(current, 15), _ema(prior, 9), _ema(prior, 15)


def _atr(candles: list[dict[str, Any]], period: int = 14) -> float:
    if len(candles) < 2:
        return 0.0
    start = max(1, len(candles) - period)
    values = []
    for i in range(start, len(candles)):
        high = float(candles[i]["h"])
        low = float(candles[i]["l"])
        previous_close = float(candles[i - 1]["c"])
        values.append(max(high - low, abs(high - previous_close), abs(low - previous_close)))
    return sum(values) / len(values) if values else 0.0


def _body(candle: dict[str, Any]) -> tuple[float, float, float, float]:
    op, high, low, close = (float(candle[k]) for k in ("o", "h", "l", "c"))
    span = max(high - low, 1e-12)
    return abs(close - op), (close - low) / span, high - max(op, close), min(op, close) - low


def _trend_state(candles: list[dict[str, Any]]) -> dict[str, Any]:
    if len(candles) < MIN_BARS:
        return {"direction": "INSUFFICIENT DATA", "strength": "INSUFFICIENT DATA", "ready": False, "reason": f"Need {MIN_BARS} closed candles; have {len(candles)}."}
    ema9, ema15, prior9, prior15 = _ema_pair(candles)
    atr = _atr(candles)
    close = float(candles[-1]["c"])
    if atr <= 0:
        return {"direction": "FLAT", "strength": "WEAK", "ready": True, "reason": "ATR is unavailable or zero.", "ema9": ema9, "ema15": ema15, "atr": atr}
    separation = abs(ema9 - ema15) / atr
    slope9, slope15 = (ema9 - prior9) / atr, (ema15 - prior15) / atr
    if separation < 0.035 or (abs(slope9) < 0.004 and abs(slope15) < 0.004):
        direction = "FLAT"
    elif ema9 > ema15 and slope9 > 0 and slope15 > -0.003:
        direction = "BULLISH"
    elif ema9 < ema15 and slope9 < 0 and slope15 < 0.003:
        direction = "BEARISH"
    else:
        direction = "FLAT"
    if separation >= 0.18 and abs(slope9) >= 0.025 and abs(slope15) >= 0.012:
        strength = "STRONG"
    elif separation >= 0.075:
        strength = "MODERATE"
    else:
        strength = "WEAK"
    reason = f"EMA9 {ema9:.8g} / EMA15 {ema15:.8g}; separation {separation:.3f} ATR; slopes {slope9:.3f}/{slope15:.3f} ATR; close {close:.8g}."
    return {
        "direction": direction, "strength": strength, "ready": True, "reason": reason,
        "ema9": ema9, "ema15": ema15, "prior_ema9": prior9, "prior_ema15": prior15,
        "slope9_atr": slope9, "slope15_atr": slope15, "separation_atr": separation,
        "atr": atr, "close": close,
    }


def _confirmed_swings(candles: list[dict[str, Any]], left: int = 2, right: int = 2, lookback: int = 120) -> tuple[list[tuple[int, float]], list[tuple[int, float]]]:
    highs = [float(x["h"]) for x in candles]
    lows = [float(x["l"]) for x in candles]
    end = len(candles) - right
    start = max(left, end - lookback)
    pivot_highs: list[tuple[int, float]] = []
    pivot_lows: list[tuple[int, float]] = []
    for j in range(start, end):
        high_window = highs[j - left:j + right + 1]
        low_window = lows[j - left:j + right + 1]
        if len(high_window) != left + right + 1:
            continue
        if highs[j] == max(high_window) and highs[j] > highs[j - 1] and highs[j] >= highs[j + 1]:
            pivot_highs.append((j, highs[j]))
        if lows[j] == min(low_window) and lows[j] < lows[j - 1] and lows[j] <= lows[j + 1]:
            pivot_lows.append((j, lows[j]))
    return pivot_highs, pivot_lows


def _rejection(candle: dict[str, Any], side: str, atr: float, min_body_atr: float = 0.12) -> bool:
    body, close_position, upper_wick, lower_wick = _body(candle)
    if atr <= 0 or body < atr * min_body_atr:
        return False
    if side == "BUY":
        return float(candle["c"]) > float(candle["o"]) and lower_wick >= max(body * 0.70, atr * 0.08) and close_position >= 0.66
    return float(candle["c"]) < float(candle["o"]) and upper_wick >= max(body * 0.70, atr * 0.08) and close_position <= 0.34


def _recent_cross(candles: list[dict[str, Any]], side: str, bars: int = 3) -> bool:
    if len(candles) < MIN_BARS + 1:
        return False
    start = max(1, len(candles) - bars)
    for end in range(start, len(candles)):
        current = _ema_pair(candles[:end + 1])
        previous = _ema_pair(candles[:end])
        if side == "BUY" and previous[0] <= previous[1] and current[0] > current[1]:
            return True
        if side == "SELL" and previous[0] >= previous[1] and current[0] < current[1]:
            return True
    return False


def _middle_setup(candles: list[dict[str, Any]], side: str) -> dict[str, Any]:
    trend = _trend_state(candles)
    result = {"status": "WAITING", "direction": trend["direction"], "pullback_confirmed": False, "momentum": "UNKNOWN", "reason": trend["reason"], "trend": trend}
    if not trend["ready"]:
        result["status"] = "INSUFFICIENT DATA"
        return result
    expected = "BULLISH" if side == "BUY" else "BEARISH"
    if trend["direction"] != expected:
        result["status"] = "SETUP REJECTED"
        result["reason"] = f"{trend['direction']} EMA regime does not agree with {side} higher-timeframe direction."
        return result
    atr, ema9, ema15 = float(trend["atr"]), float(trend["ema9"]), float(trend["ema15"])
    current = candles[-1]
    bodies = [abs(float(x["c"]) - float(x["o"])) for x in candles[-6:-1]]
    average_body = sum(bodies) / len(bodies) if bodies else 0.0
    current_body, _position, _upper, _lower = _body(current)
    zone_low, zone_high = min(ema9, ema15) - atr * 0.25, max(ema9, ema15) + atr * 0.20
    recent = candles[-4:]
    if side == "BUY":
        touched = any(float(x["l"]) <= zone_high and float(x["h"]) >= zone_low for x in recent)
        rejected = _rejection(current, side, atr) and float(current["c"]) > ema9
    else:
        touched = any(float(x["h"]) >= zone_low and float(x["l"]) <= zone_high for x in recent)
        rejected = _rejection(current, side, atr) and float(current["c"]) < ema9
    pullback_confirmed = bool(touched and rejected)
    crossed = _recent_cross(candles, side, 3)
    spread_now = abs(ema9 - ema15)
    previous_pair = _ema_pair(candles[:-1])
    spread_previous = abs(previous_pair[0] - previous_pair[1])
    expansion = current_body >= max(average_body * 1.05, atr * 0.12) or spread_now > spread_previous * 1.005
    weakening = (side == "BUY" and float(current["c"]) < ema9) or (side == "SELL" and float(current["c"]) > ema9)
    if current_body < atr * 0.06 and not crossed:
        weakening = True
    setup_ok = (crossed or pullback_confirmed) and expansion and not weakening
    result.update({
        "status": "BULLISH SETUP" if setup_ok and side == "BUY" else "BEARISH SETUP" if setup_ok else "SETUP REJECTED" if weakening else "WAITING",
        "pullback_confirmed": pullback_confirmed,
        "momentum": "EXPANDING" if expansion else "WEAKENING",
        "rejection_confirmed": bool(rejected),
        "recent_ema_crossover": bool(crossed),
        "reason": f"{'EMA crossover' if crossed else 'EMA-zone pullback + rejection' if pullback_confirmed else 'No fresh crossover or confirmed pullback'}; momentum {'expanding' if expansion else 'weak/contracting'}; EMA9/15 {ema9:.8g}/{ema15:.8g}.",
    })
    return result


def _pullback_confirmation(candles: list[dict[str, Any]], side: str) -> dict[str, Any]:
    trend = _trend_state(candles)
    result = {"status": "WAITING", "pullback_confirmed": False, "reason": trend["reason"], "trend": trend}
    if not trend["ready"]:
        result["status"] = "INSUFFICIENT DATA"
        return result
    if trend["strength"] == "WEAK":
        result["status"] = "SETUP REJECTED"
        result["reason"] = "5M EMA separation/slope is weak or sideways; pullback entry is blocked."
        return result
    expected = "BULLISH" if side == "BUY" else "BEARISH"
    if trend["direction"] != expected:
        result["status"] = "SETUP REJECTED"
        result["reason"] = f"5M EMA direction {trend['direction']} conflicts with {side}."
        return result
    atr, ema9, ema15 = float(trend["atr"]), float(trend["ema9"]), float(trend["ema15"])
    current = candles[-1]
    lower, upper = min(ema9, ema15) - atr * 0.25, max(ema9, ema15) + atr * 0.20
    recent = candles[-4:]
    if side == "BUY":
        touched = any(float(x["l"]) <= upper and float(x["h"]) >= lower for x in recent)
        reclaimed = float(current["c"]) > ema9 and _rejection(current, side, atr, 0.10)
    else:
        touched = any(float(x["h"]) >= lower and float(x["l"]) <= upper for x in recent)
        reclaimed = float(current["c"]) < ema9 and _rejection(current, side, atr, 0.10)
    passed = touched and reclaimed
    result.update({
        "status": "PULLBACK CONFIRMED" if passed else "WAITING",
        "pullback_confirmed": bool(passed),
        "reason": "5M EMA-zone pullback was rejected in the trend direction." if passed else "Waiting for a 5M EMA-zone retest and closed-candle rejection.",
    })
    return result


def _entry_trigger(candles: list[dict[str, Any]], side: str) -> dict[str, Any]:
    result = {"status": "WAITING", "side": None, "trigger_price": None, "stop_anchor": None, "reason": "Waiting for 1M rejection, confirmed micro BOS and trigger break."}
    if len(candles) < MIN_BARS + 2:
        result["status"] = "INSUFFICIENT DATA"
        result["reason"] = f"Need {MIN_BARS + 2} completed 1M candles; have {len(candles)}."
        return result
    confirmation, candidate = candles[-1], candles[-2]
    candidate_prefix = candles[:-1]
    trend = _trend_state(candidate_prefix)
    if not trend["ready"] or trend["strength"] == "WEAK" or trend["direction"] != ("BULLISH" if side == "BUY" else "BEARISH"):
        result["reason"] = "1M EMA9/15 alignment, slope and trend strength are not confirmed in the required direction."
        return result
    atr, ema9, ema15 = float(trend["atr"]), float(trend["ema9"]), float(trend["ema15"])
    zone_low, zone_high = min(ema9, ema15) - atr * 0.25, max(ema9, ema15) + atr * 0.20
    if side == "BUY":
        zone_touch = float(candidate["l"]) <= zone_high and float(candidate["h"]) >= zone_low
        rejection = _rejection(candidate, side, atr) and float(candidate["c"]) > ema9
    else:
        zone_touch = float(candidate["h"]) >= zone_low and float(candidate["l"]) <= zone_high
        rejection = _rejection(candidate, side, atr) and float(candidate["c"]) < ema9
    pivot_highs, pivot_lows = _confirmed_swings(candidate_prefix)
    last_high = pivot_highs[-1] if pivot_highs else None
    last_low = pivot_lows[-1] if pivot_lows else None
    bos = bool(last_high and float(candidate["c"]) > last_high[1]) if side == "BUY" else bool(last_low and float(candidate["c"]) < last_low[1])
    if not (zone_touch and rejection and bos):
        result["reason"] = f"1M candidate rejected: EMA pullback={bool(zone_touch)}, rejection={bool(rejection)}, confirmed micro BOS={bool(bos)}."
        return result
    confirmation_body, confirmation_position, _cu, _cl = _body(confirmation)
    current_ema9, current_ema15, _p9, _p15 = _ema_pair(candles)
    current_close = float(confirmation["c"])
    if side == "BUY":
        trigger = float(candidate["h"])
        trigger_broken = current_close > trigger
        aligned = current_ema9 > current_ema15 and current_close > current_ema9
        strong = float(confirmation["c"]) > float(confirmation["o"]) and confirmation_body >= atr * 0.10 and confirmation_position >= 0.62
        extended = current_close - current_ema9 > atr * 1.50
        stop_swing = last_low[1] if last_low else None
        stop = min(float(candidate["l"]), stop_swing) - atr * 0.10 if stop_swing is not None else None
    else:
        trigger = float(candidate["l"])
        trigger_broken = current_close < trigger
        aligned = current_ema9 < current_ema15 and current_close < current_ema9
        strong = float(confirmation["c"]) < float(confirmation["o"]) and confirmation_body >= atr * 0.10 and confirmation_position <= 0.38
        extended = current_ema9 - current_close > atr * 1.50
        stop_swing = last_high[1] if last_high else None
        stop = max(float(candidate["h"]), stop_swing) + atr * 0.10 if stop_swing is not None else None
    if not trigger_broken:
        result["reason"] = f"1M trigger not closed beyond candidate candle level {trigger:.8g}."
        return result
    if not aligned or not strong:
        result["reason"] = "1M EMA alignment or strong closed confirmation candle failed."
        return result
    if extended:
        result["reason"] = "Entry rejected: price is more than 1.5 ATR away from EMA9."
        return result
    if stop is None or (side == "BUY" and stop >= current_close) or (side == "SELL" and stop <= current_close):
        result["status"] = "SETUP REJECTED"
        result["reason"] = "No valid recent confirmed swing available for structural stop-loss."
        return result
    risk = current_close - stop if side == "BUY" else stop - current_close
    if risk <= 0 or risk > atr * 5.0:
        result["status"] = "SETUP REJECTED"
        result["reason"] = "Structural stop distance is invalid or wider than 5 ATR."
        return result
    result.update({
        "status": side, "side": side, "trigger_price": trigger, "stop_anchor": stop,
        "candidate_candle_time": int(candidate["t"]),
        "confirmation_candle_time": int(confirmation["t"]),
        "micro_bos": True, "pullback_rejection": True,
        "reason": f"1M EMA retest rejection + confirmed micro BOS + close through trigger {trigger:.8g}.",
    })
    return result


def _clean_candles(candles: Any) -> list[dict[str, float | int]]:
    by_time: dict[int, dict[str, float | int]] = {}
    if candles is None:
        return []
    for row in candles:
        try:
            stamp = int(row.get("t", 0))
            op, high, low, close = (_number(row.get(k), float("nan")) for k in ("o", "h", "l", "c"))
            volume = max(0.0, _number(row.get("v")))
            if stamp <= 0 or any(x != x or abs(x) == float("inf") for x in (op, high, low, close)):
                continue
            if low <= 0 or high < max(op, close, low) or low > min(op, close, high):
                continue
            by_time[stamp] = {"t": stamp, "o": op, "h": high, "l": low, "c": close, "v": volume}
        except (TypeError, ValueError, AttributeError):
            continue
    return [by_time[key] for key in sorted(by_time)][-BACKTEST_LIMIT:]


def _summary(trades: list[dict[str, Any]], bars: int, status_override: str | None = None) -> dict[str, Any]:
    wins = [x for x in trades if x["result"] == "WIN"]
    losses = [x for x in trades if x["result"] == "LOSS"]
    breakeven = [x for x in trades if x["result"] == "BREAK_EVEN"]
    net_values = [_number(x.get("net_result_r", x.get("result_r"))) for x in trades]
    gross_values = [_number(x.get("result_r")) for x in trades]
    gross_wins = [x for x in gross_values if x > 0]
    gross_losses = [abs(x) for x in gross_values if x < 0]
    net_wins = [x for x in net_values if x > 0]
    net_losses = [abs(x) for x in net_values if x < 0]
    equity = peak = max_dd = 0.0
    win_streak = loss_streak = max_win_streak = max_loss_streak = 0
    for trade, value in zip(trades, net_values):
        equity += value
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
        if trade["result"] == "WIN":
            win_streak += 1
            loss_streak = 0
        elif trade["result"] == "LOSS":
            loss_streak += 1
            win_streak = 0
        else:
            win_streak = loss_streak = 0
        max_win_streak = max(max_win_streak, win_streak)
        max_loss_streak = max(max_loss_streak, loss_streak)
    closed = len(trades)
    status = status_override or ("INSUFFICIENT DATA" if bars < MIN_BARS else "NO CLOSED TRADES" if closed == 0 else "CALCULATED")
    average_win = sum(gross_wins) / len(gross_wins) if gross_wins else None
    average_loss = sum(gross_losses) / len(gross_losses) if gross_losses else None
    return {
        "status": status, "bars_tested": bars, "total_trades": closed, "total_historical_trades": closed,
        "winning_trades": len(wins), "losing_trades": len(losses), "break_even_trades": len(breakeven),
        "total_winning_trades": len(wins), "total_losing_trades": len(losses),
        "win_rate_pct": round(len(wins) / closed * 100.0, 2) if closed else None,
        "loss_rate_pct": round(len(losses) / closed * 100.0, 2) if closed else None,
        "actual_average_rr": round(average_win / average_loss, 4) if average_win is not None and average_loss else None,
        "actual_risk_reward_ratio": round(average_win / average_loss, 4) if average_win is not None and average_loss else None,
        "target_rr": TARGET_R, "gross_net_r": round(sum(gross_values), 4), "net_profit_loss_r": round(sum(net_values), 4),
        "maximum_drawdown_r": round(max_dd, 4), "max_consecutive_wins": max_win_streak, "max_consecutive_losses": max_loss_streak,
        "maximum_consecutive_wins": max_win_streak, "maximum_consecutive_losses": max_loss_streak,
        "profit_factor": round(sum(net_wins) / sum(net_losses), 4) if net_losses else (None if not net_wins else "INF"),
        "average_winning_r": round(average_win, 4) if average_win is not None else None,
        "average_losing_r": round(average_loss, 4) if average_loss is not None else None,
        "sample_quality": "ADEQUATE" if closed >= MIN_REPORT_TRADES else "LOW SAMPLE",
    }


class AdvancedMultiStrategyEngine:
    """AST 9/15 EMA MTF engine. Live decisions and historical replay share one evaluator."""

    def __init__(self, asset: str = "MARKET") -> None:
        self.asset = asset.upper()
        self._backtest_cache: dict[str, dict[str, Any]] = {}
        self._backtest_signatures: dict[str, tuple[Any, ...]] = {}
        self._forward_active: dict[str, dict[str, Any]] = {}
        self._forward_completed: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self._forward_seen: set[str] = set()
        self.fee_bps_per_side = max(0.0, _number(os.getenv("AST_FEE_BPS_PER_SIDE"), 0.0))
        self.spread_bps = max(0.0, _number(os.getenv("AST_HISTORICAL_SPREAD_BPS"), 0.0))
        self.slippage_bps_per_side = max(0.0, _number(os.getenv("AST_SLIPPAGE_BPS_PER_SIDE"), 0.0))

    def _signature(self, data: dict[str, list[dict[str, Any]]]) -> tuple[Any, ...]:
        values: list[Any] = []
        for tf in TIMEFRAME_ORDER:
            rows = data.get(tf, [])
            last_bar = tuple(rows[-1].get(key) for key in ("o", "h", "l", "c", "v")) if rows else None
            values.extend((tf, len(rows), int(rows[0]["t"]) if rows else None, int(rows[-1]["t"]) if rows else None, last_bar))
        return tuple(values)

    @staticmethod
    def _asof(data: dict[str, list[dict[str, Any]]], opens: dict[str, list[int]], close_time: int) -> dict[str, list[dict[str, Any]]]:
        """Keep only bars with close timestamps <= the signal candle close."""
        result: dict[str, list[dict[str, Any]]] = {}
        for tf, rows in data.items():
            end = bisect.bisect_right(opens[tf], close_time - TIMEFRAME_MS[tf]) if rows else 0
            # Signal helpers need only a bounded recent window; avoid copying up to 25k bars for every 1m replay step.
            result[tf] = rows[max(0, end - 260):end]
        return result

    def _waiting_result(self, combination: str, status: str, reason: str, stages: dict[str, Any], higher: list[dict[str, Any]], entry: dict[str, Any] | None = None) -> dict[str, Any]:
        strengths = [x.get("strength") for x in higher if x.get("strength")]
        return {
            "combination": combination, "combination_label": COMBINATIONS[combination]["label"],
            "status": status, "side": None, "reason": reason, "stages": stages,
            "higher_timeframe_trend": next((x.get("direction") for x in higher if x.get("direction")), "WAITING"),
            "higher_timeframe_strength": strengths[0] if strengths else "WAITING",
            "signal_time": int(entry.get("confirmation_candle_time", 0)) if entry else None,
            "trigger_price": entry.get("trigger_price") if entry else None,
            "stop_anchor": entry.get("stop_anchor") if entry else None,
            "entry_confirmation": entry, "trade_plan": None,
        }

    def _evaluate_combination(self, data: dict[str, list[dict[str, Any]]], combination: str) -> dict[str, Any]:
        spec = COMBINATIONS[combination]
        entry_candles = data.get(spec["entry"], [])
        stages: dict[str, Any] = {}
        higher_states: list[dict[str, Any]] = []
        directions: list[str] = []
        blocked: list[str] = []
        for tf in spec["higher"]:
            state = _trend_state(data.get(tf, []))
            higher_states.append(state)
            stages[tf] = {
                "status": state["direction"], "direction": state["direction"], "strength": state["strength"],
                "reason": state["reason"], "ema9": state.get("ema9"), "ema15": state.get("ema15"),
                "separation_atr": state.get("separation_atr"),
            }
            if not state["ready"] or state["direction"] not in ("BULLISH", "BEARISH") or state["strength"] == "WEAK":
                blocked.append(tf)
            else:
                directions.append(state["direction"])
        if blocked:
            insuff = any(not state["ready"] for state in higher_states)
            status = "INSUFFICIENT DATA" if insuff else "WAITING"
            return self._waiting_result(combination, status, f"HTF flat/weak or history missing: {', '.join(blocked)}.", stages, higher_states)
        if len(set(directions)) != 1:
            return self._waiting_result(combination, "HTF CONFLICT", "Higher-timeframe EMA direction conflicts. No trade is allowed.", stages, higher_states)
        side = "BUY" if directions[0] == "BULLISH" else "SELL"

        middle_states = []
        for tf in spec["middle"]:
            state = _middle_setup(data.get(tf, []), side)
            middle_states.append(state)
            stages[tf] = {
                "status": state["status"], "direction": state["direction"],
                "pullback_confirmed": state["pullback_confirmed"], "momentum": state["momentum"],
                "reason": state["reason"],
            }
        if any(x["status"] == "INSUFFICIENT DATA" for x in middle_states):
            return self._waiting_result(combination, "INSUFFICIENT DATA", "Middle timeframe candle history is insufficient.", stages, higher_states)
        if not all(x["status"] in ("BULLISH SETUP", "BEARISH SETUP") for x in middle_states):
            return self._waiting_result(combination, "WAITING", "30M/15M setup confirmation is incomplete or rejected.", stages, higher_states)
        if any(x["direction"] != ("BULLISH" if side == "BUY" else "BEARISH") for x in middle_states):
            return self._waiting_result(combination, "WAITING", "Middle timeframe direction does not agree with HTF.", stages, higher_states)

        pullback_tf = spec["pullback"]
        pullback = _pullback_confirmation(data.get(pullback_tf, []), side)
        stages[pullback_tf] = {"status": pullback["status"], "pullback_confirmed": pullback["pullback_confirmed"], "reason": pullback["reason"]}
        if pullback["status"] == "INSUFFICIENT DATA":
            return self._waiting_result(combination, "INSUFFICIENT DATA", "5M history is insufficient.", stages, higher_states)
        if pullback["status"] != "PULLBACK CONFIRMED":
            return self._waiting_result(combination, "WAITING", "5M pullback confirmation is missing.", stages, higher_states)

        entry = _entry_trigger(entry_candles, side)
        stages[spec["entry"]] = {
            "status": entry["status"], "trigger_price": entry.get("trigger_price"),
            "micro_bos": entry.get("micro_bos", False), "pullback_rejection": entry.get("pullback_rejection", False),
            "reason": entry["reason"],
        }
        if entry["status"] == "INSUFFICIENT DATA":
            return self._waiting_result(combination, "INSUFFICIENT DATA", "1M history is insufficient.", stages, higher_states)
        if entry["side"] != side:
            return self._waiting_result(combination, "WAITING", entry["reason"], stages, higher_states, entry=entry)
        return {
            "combination": combination, "combination_label": spec["label"], "status": side, "side": side,
            "reason": f"All mandatory confirmations passed: {' → '.join([*spec['higher'], *spec['middle'], pullback_tf, spec['entry']])}.",
            "stages": stages, "higher_timeframe_trend": side,
            "higher_timeframe_strength": min((x["strength"] for x in higher_states), key=lambda x: {"STRONG": 3, "MODERATE": 2, "WEAK": 1}.get(x, 0)),
            "signal_time": int(entry_candles[-1]["t"]) if entry_candles else None,
            "trigger_price": entry.get("trigger_price"), "stop_anchor": entry.get("stop_anchor"),
            "entry_confirmation": entry, "trade_plan": None,
        }

    def _cost_in_price(self, entry: float) -> float:
        total_bps = self.fee_bps_per_side * 2.0 + self.spread_bps + self.slippage_bps_per_side * 2.0
        return entry * total_bps / 10_000.0

    def _simulate_trade(self, candles: list[dict[str, Any]], entry_index: int, side: str, entry: float, stop: float, signal_time: int, combination: str) -> dict[str, Any] | None:
        risk = entry - stop if side == "BUY" else stop - entry
        if risk <= 0 or entry <= 0 or risk > entry * 0.10:
            return None
        target = entry + TARGET_R * risk if side == "BUY" else entry - TARGET_R * risk
        for j in range(entry_index, len(candles)):
            bar = candles[j]
            op, high, low = (_number(bar.get(k)) for k in ("o", "h", "l"))
            exit_price = None
            result = None
            if (side == "BUY" and op <= stop) or (side == "SELL" and op >= stop):
                exit_price, result = op, "LOSS"
            elif (side == "BUY" and op >= target) or (side == "SELL" and op <= target):
                exit_price, result = op, "WIN"
            if exit_price is None:
                stop_hit = low <= stop if side == "BUY" else high >= stop
                target_hit = high >= target if side == "BUY" else low <= target
                if stop_hit:
                    exit_price, result = stop, "LOSS"
                elif target_hit:
                    exit_price, result = target, "WIN"
            if exit_price is None:
                continue
            gross_r = TARGET_R if result == "WIN" else -1.0
            realized_r = (exit_price - entry) / risk if side == "BUY" else (entry - exit_price) / risk
            cost_r = self._cost_in_price(entry) / risk
            return {
                "trade_id": f"{self.asset}:{combination}:{signal_time}:{side}", "asset": self.asset,
                "combination": combination, "combination_label": COMBINATIONS[combination]["label"],
                "side": side, "signal_time": signal_time, "entry_time": int(bar.get("t", 0)),
                "exit_time": int(bar.get("t", 0)), "entry_price": round(entry, 8),
                "stop_loss": round(stop, 8), "take_profit": round(target, 8), "exit_price": round(exit_price, 8),
                "result": result, "result_r": round(gross_r, 4), "realized_price_r": round(realized_r, 4),
                "cost_r": round(cost_r, 6), "net_result_r": round(realized_r - cost_r, 4),
                "exit_reason": "STOP_LOSS" if result == "LOSS" else "TAKE_PROFIT",
            }
        return None

    def _backtest(self, data: dict[str, list[dict[str, Any]]], combination: str) -> dict[str, Any]:
        minute = data.get("1m", [])
        needed = set((*COMBINATIONS[combination]["higher"], *COMBINATIONS[combination]["middle"], "5m", "1m"))
        missing = [tf for tf in sorted(needed) if len(data.get(tf, [])) < MIN_BARS]
        if len(minute) < MIN_BARS + 2 or missing:
            reason = "Not enough completed 1M candles." if len(minute) < MIN_BARS + 2 else f"Insufficient closed history for: {', '.join(missing)}."
            return {
                "combination": combination, "combination_label": COMBINATIONS[combination]["label"],
                "historical": _summary([], len(minute), "INSUFFICIENT DATA"),
                "in_sample": _summary([], 0, "INSUFFICIENT DATA"), "out_of_sample": _summary([], 0, "INSUFFICIENT DATA"),
                "trades": [], "reason": reason,
            }
        opens = {tf: [int(x["t"]) for x in rows] for tf, rows in data.items()}
        start_time, end_time = int(minute[0]["t"]), int(minute[-1]["t"]) + TIMEFRAME_MS["1m"]
        split_time = start_time + int((end_time - start_time) * 0.70)
        trades: list[dict[str, Any]] = []
        next_available = MIN_BARS + 1
        open_setups = 0
        for i in range(MIN_BARS + 1, len(minute) - 1):
            if i < next_available:
                continue
            signal_close_time = int(minute[i]["t"]) + TIMEFRAME_MS["1m"]
            view = self._asof(data, opens, signal_close_time)
            evaluation = self._evaluate_combination(view, combination)
            side, stop, trigger = evaluation.get("side"), evaluation.get("stop_anchor"), evaluation.get("trigger_price")
            if not side or stop is None or trigger is None:
                continue
            entry_index = i + 1
            next_open = float(minute[entry_index]["o"])
            if (side == "BUY" and next_open < float(trigger)) or (side == "SELL" and next_open > float(trigger)):
                continue
            if (side == "BUY" and float(stop) >= next_open) or (side == "SELL" and float(stop) <= next_open):
                continue
            trade = self._simulate_trade(minute, entry_index, side, next_open, float(stop), signal_close_time, combination)
            if trade is None:
                open_setups += 1
                next_available = len(minute)
                continue
            trade["sample"] = "IN_SAMPLE" if signal_close_time < split_time else "OUT_OF_SAMPLE"
            trades.append(trade)
            exit_index = next((j for j in range(entry_index, len(minute)) if int(minute[j]["t"]) == trade["exit_time"]), entry_index)
            next_available = exit_index + 1

        historical = _summary(trades, len(minute))
        split_index = bisect.bisect_left(opens["1m"], split_time)
        in_trades = [x for x in trades if x["sample"] == "IN_SAMPLE"]
        out_trades = [x for x in trades if x["sample"] == "OUT_OF_SAMPLE"]
        in_sample = _summary(in_trades, split_index)
        out_sample = _summary(out_trades, len(minute) - split_index)
        if len(in_trades) < MIN_SPLIT_TRADES:
            in_sample["status"] = "INSUFFICIENT DATA"
            in_sample["win_rate_pct"] = None
        if len(out_trades) < MIN_SPLIT_TRADES:
            out_sample["status"] = "INSUFFICIENT DATA"
            out_sample["win_rate_pct"] = None
        historical["open_unclosed_setups"] = open_setups
        costs_applied = bool(self.fee_bps_per_side or self.spread_bps or self.slippage_bps_per_side)
        historical["costs"] = {
            "fee_bps_per_side": self.fee_bps_per_side, "spread_bps": self.spread_bps,
            "slippage_bps_per_side": self.slippage_bps_per_side, "included": costs_applied,
            "reason": "Configured estimates applied from AST_FEE_BPS_PER_SIDE, AST_HISTORICAL_SPREAD_BPS and AST_SLIPPAGE_BPS_PER_SIDE." if costs_applied else "Historical bid/ask spread, account fee tier and execution slippage are not in candle OHLC data; no costs were invented or applied.",
        }
        historical["trades"] = trades
        return {
            "combination": combination, "combination_label": COMBINATIONS[combination]["label"],
            "historical": historical, "in_sample": in_sample, "out_of_sample": out_sample,
            "trades": trades,
            "split_method": "Chronological 70% in-sample / 30% out-of-sample; no parameters are fitted on the test period.",
            "reason": None,
        }

    def _forward_update(self, combination: str, one_minute: list[dict[str, Any]], live: dict[str, Any], live_price: float | None) -> dict[str, Any]:
        active = self._forward_active.get(combination)
        if active:
            last_seen = int(active.get("last_processed_time", active["signal_time"]))
            for bar in one_minute:
                stamp = int(bar["t"])
                if stamp <= last_seen:
                    continue
                op, high, low = float(bar["o"]), float(bar["h"]), float(bar["l"])
                side, stop, target = active["side"], float(active["stop_loss"]), float(active["take_profit"])
                exit_price = None
                outcome = None
                if (side == "BUY" and op <= stop) or (side == "SELL" and op >= stop):
                    exit_price, outcome = op, "LOSS"
                elif (side == "BUY" and op >= target) or (side == "SELL" and op <= target):
                    exit_price, outcome = op, "WIN"
                else:
                    stop_hit = low <= stop if side == "BUY" else high >= stop
                    target_hit = high >= target if side == "BUY" else low <= target
                    if stop_hit:
                        exit_price, outcome = stop, "LOSS"
                    elif target_hit:
                        exit_price, outcome = target, "WIN"
                active["last_processed_time"] = stamp
                if exit_price is not None and outcome:
                    risk, entry = float(active["risk_distance"]), float(active["entry_price"])
                    realized = (exit_price - entry) / risk if side == "BUY" else (entry - exit_price) / risk
                    cost_r = self._cost_in_price(entry) / risk
                    active.update({
                        "exit_time": stamp, "exit_price": exit_price, "result": outcome,
                        "result_r": TARGET_R if outcome == "WIN" else -1.0,
                        "realized_price_r": realized, "cost_r": cost_r, "net_result_r": realized - cost_r,
                    })
                    self._forward_completed[combination].append(active.copy())
                    if len(self._forward_completed[combination]) > 1000:
                        self._forward_completed[combination] = self._forward_completed[combination][-1000:]
                    self._forward_active.pop(combination, None)
                    break

        if combination not in self._forward_active and live.get("side") and live.get("stop_anchor") is not None and live.get("signal_time") is not None and live_price is not None and live_price > 0:
            signal_id = f"{self.asset}:{combination}:{live['signal_time']}:{live['side']}"
            side, entry = live["side"], float(live_price)
            trigger, stop = float(live["trigger_price"]), float(live["stop_anchor"])
            valid_trigger = entry >= trigger if side == "BUY" else entry <= trigger
            risk = entry - stop if side == "BUY" else stop - entry
            if signal_id not in self._forward_seen and valid_trigger and risk > 0 and risk <= entry * 0.10:
                target = entry + TARGET_R * risk if side == "BUY" else entry - TARGET_R * risk
                self._forward_active[combination] = {
                    "trade_id": signal_id, "asset": self.asset, "combination": combination, "side": side,
                    "signal_time": int(live["signal_time"]), "entry_time": int(live["signal_time"]) + TIMEFRAME_MS["1m"],
                    "last_processed_time": int(live["signal_time"]), "entry_price": entry,
                    "stop_loss": stop, "take_profit": target, "risk_distance": risk, "result": "OPEN",
                }
                self._forward_seen.add(signal_id)

        completed = self._forward_completed[combination]
        summary = _summary(completed, len(one_minute))
        summary["active_trades"] = 1 if combination in self._forward_active else 0
        summary["tracking"] = "PROCESS MEMORY · PAPER ONLY · RESETS ON SERVICE RESTART"
        summary["status"] = "INSUFFICIENT DATA" if len(completed) < MIN_REPORT_TRADES else "FORWARD TESTING"
        if len(completed) < MIN_REPORT_TRADES:
            summary["win_rate_pct"] = None
        return summary

    def snapshot(self, candles_by_tf: dict[str, Any], live_price: float | None = None, live_spread: float | None = None) -> dict[str, Any]:
        data = {tf: _clean_candles(candles_by_tf.get(tf, [])) for tf in TIMEFRAME_ORDER}
        signature = self._signature(data)
        if any(self._backtest_signatures.get(combo) != signature for combo in COMBINATIONS):
            for combo in COMBINATIONS:
                self._backtest_cache[combo] = self._backtest(data, combo)
                self._backtest_signatures[combo] = signature
        one_minute = data.get("1m", [])
        if one_minute:
            eval_time = int(one_minute[-1]["t"]) + TIMEFRAME_MS["1m"]
            opens = {tf: [int(x["t"]) for x in rows] for tf, rows in data.items()}
            asof = self._asof(data, opens, eval_time)
        else:
            eval_time, asof = None, data

        combinations: dict[str, Any] = {}
        for combo in COMBINATIONS:
            live = self._evaluate_combination(asof, combo)
            live["evaluation_close_time"] = eval_time
            if live.get("side") and live.get("stop_anchor") is not None and live.get("trigger_price") is not None:
                entry = _number(live_price, _number(one_minute[-1]["c"])) if one_minute else 0.0
                trigger, stop, side = float(live["trigger_price"]), float(live["stop_anchor"]), live["side"]
                valid_trigger = entry >= trigger if side == "BUY" else entry <= trigger
                risk = entry - stop if side == "BUY" else stop - entry
                if not valid_trigger or risk <= 0:
                    live["status"], live["side"] = "WAITING", None
                    live["reason"] = "Trigger was crossed but live price has returned through the trigger/stop. Pending setup cancelled."
                else:
                    target = entry + TARGET_R * risk if side == "BUY" else entry - TARGET_R * risk
                    live["trade_plan"] = {
                        "side": side, "entry_price": round(entry, 8), "trigger_price": round(trigger, 8),
                        "stop_loss": round(stop, 8), "take_profit": round(target, 8), "risk_reward": TARGET_R,
                        "risk_distance": round(risk, 8), "signal_time": live.get("signal_time"), "timeframe": "1m",
                        "execution": "Closed 1M confirmation followed by live reference price; paper plan only.",
                        "live_spread": live_spread,
                    }
            live["forward_testing"] = self._forward_update(combo, one_minute, live, live_price)
            combinations[combo] = {
                "combination": combo, "label": COMBINATIONS[combo]["label"], "live": live,
                "historical": self._backtest_cache[combo]["historical"],
                "in_sample": self._backtest_cache[combo]["in_sample"],
                "out_of_sample": self._backtest_cache[combo]["out_of_sample"],
                "forward_testing": live["forward_testing"], "backtest_reason": self._backtest_cache[combo].get("reason"),
            }
        reports = [self._backtest_cache[x] for x in COMBINATIONS]
        all_trades = [trade for report in reports for trade in report.get("trades", [])]
        combined_historical = _summary(all_trades, len(one_minute))
        all_forward = [trade for combo in COMBINATIONS for trade in self._forward_completed[combo]]
        combined_forward = _summary(all_forward, len(one_minute))
        combined_forward["status"] = "INSUFFICIENT DATA" if len(all_forward) < MIN_REPORT_TRADES else "FORWARD TESTING"
        if len(all_forward) < MIN_REPORT_TRADES:
            combined_forward["win_rate_pct"] = None
        return {
            "asset": self.asset, "strategy_name": "AST 9/15 EMA Advanced Multi-Timeframe Trading Engine",
            "status": "READY" if one_minute and len(one_minute) >= MIN_BARS else "INSUFFICIENT DATA",
            "live_reference_price": live_price, "combinations": combinations,
            "overall_historical": combined_historical, "overall_forward_testing": combined_forward,
            "timeframe_data": {tf: len(data.get(tf, [])) for tf in TIMEFRAME_ORDER},
            "backtest_scope": {
                "combinations": {key: COMBINATIONS[key]["label"] for key in COMBINATIONS},
                "maximum_bars_per_timeframe": BACKTEST_LIMIT, "closed_candles_only": True,
                "shared_live_and_historical_evaluator": True,
                "split": "70% chronological in-sample / 30% out-of-sample",
            },
        }

    def historical_trades(self, combination: str) -> dict[str, Any]:
        key = str(combination).upper()
        if key not in COMBINATIONS:
            return {"combination": key, "status": "INSUFFICIENT DATA", "trades": [], "reason": "Unsupported combination. Use A, B or C."}
        return self._backtest_cache.get(key, {
            "combination": key, "status": "INSUFFICIENT DATA", "trades": [], "reason": "Backtest has not completed yet.",
        })


def aggregate_reports(reports: list[dict[str, Any]], label: str = "OVERALL COMBINED") -> dict[str, Any]:
    trades = [trade for report in reports for trade in report.get("trades", [])]
    bars = sum(int((report.get("historical") or {}).get("bars_tested", 0)) for report in reports)
    return {
        "label": label, "historical": _summary(trades, bars),
        "in_sample": _summary([x for x in trades if x.get("sample") == "IN_SAMPLE"], bars),
        "out_of_sample": _summary([x for x in trades if x.get("sample") == "OUT_OF_SAMPLE"], bars),
    }
