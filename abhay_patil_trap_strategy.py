from __future__ import annotations

import copy
import math
from typing import Any


class AbhayPatilBlackBoxTrapStrategy:
    """Replay the uploaded Abhay Patil Black Box Pine v5 strategy on closed OHLC bars.

    Source rules preserved:
      * ta.pivothigh/ta.pivotlow with a 20-bar left/right confirmation
      * close crossunder support arms a bear-trap alert
      * close crossover resistance arms a bull-trap alert
      * a later green/red reclaim candle creates a stop order at its high/low
      * stop is the opposite reclaim-candle wick
      * one take-profit at 2R (the source has one TP, not TP1 + TP2)

    This is an OHLC replay approximation, not TradingView's broker emulator.
    If SL and TP are both touched within one candle, SL is counted first.
    """

    TIMEFRAMES = ("1d", "4h", "1h", "30m", "15m", "5m", "1m")
    LOOKBACK = 20
    RR_RATIO = 2.0

    def __init__(self) -> None:
        self._cache_signature: tuple[Any, ...] | None = None
        self._cache_result: dict[str, Any] | None = None

    @staticmethod
    def _num(row: dict[str, Any], key: str) -> float | None:
        try:
            value = float(row[key])
            return value if math.isfinite(value) else None
        except (KeyError, TypeError, ValueError):
            return None

    @classmethod
    def _clean_bars(cls, source: Any) -> list[dict[str, Any]]:
        unique: dict[Any, dict[str, Any]] = {}
        for index, row in enumerate(list(source or [])):
            opened = cls._num(row, "o")
            high = cls._num(row, "h")
            low = cls._num(row, "l")
            close = cls._num(row, "c")
            if None in (opened, high, low, close) or high < low:
                continue
            stamp = row.get("t")
            key = stamp if stamp is not None else ("index", index)
            unique[key] = {
                "t": int(stamp) if isinstance(stamp, (int, float)) else None,
                "o": opened, "h": high, "l": low, "c": close,
                "v": cls._num(row, "v") or 0.0,
            }
        rows = list(unique.values())
        rows.sort(key=lambda item: item["t"] if item["t"] is not None else -1)
        return rows

    @staticmethod
    def _public_signal(signal: dict[str, Any] | None, active: bool = False) -> dict[str, Any] | None:
        if signal is None:
            return None
        result = {
            key: value for key, value in signal.items()
            if key not in ("signal_index", "eligible_from_index", "fill_index")
        }
        side = str(signal["side"])
        trigger = float(signal["entry_trigger"])
        stop = float(signal["stop_loss"])
        target = float(signal["take_profit"])
        result["trade_plan"] = {
            "side": side,
            "order_type": "BUY STOP" if side == "BUY" else "SELL STOP",
            "entry_price": trigger,
            "entry_low": trigger,
            "entry_high": trigger,
            "stop_loss": stop,
            "take_profit": target,
            "rr_ratio": float(signal["rr_ratio"]),
            "fill_price": signal.get("fill_price"),
            "entry_state": "TRADE ACTIVE" if active else (
                "BUY STOP PENDING" if side == "BUY" else "SELL STOP PENDING"
            ),
            "instruction": (
                "Stop-entry order is filled; the source strategy uses one 1:2 target."
                if active else
                "Wait for a later candle to trigger the reclaim-candle high/low stop order; do not enter at the reclaim close."
            ),
        }
        return result

    @classmethod
    def replay_timeframe(
        cls,
        timeframe: str,
        source: Any,
        lookback: int = LOOKBACK,
        rr_ratio: float = RR_RATIO,
    ) -> dict[str, Any]:
        bars = cls._clean_bars(source)
        minimum = lookback * 2 + 1
        if len(bars) < minimum:
            return {
                "strategy_name": "Abhay Patil - Black Box (Buy & Sell)",
                "timeframe": timeframe,
                "status": "WARMING UP",
                "lookback": lookback,
                "rr_ratio": rr_ratio,
                "bars_scanned": len(bars),
                "first_bar_time": bars[0]["t"] if bars else None,
                "last_bar_time": bars[-1]["t"] if bars else None,
                "support_level": None,
                "resistance_level": None,
                "bear_trap_alert": False,
                "bull_trap_alert": False,
                "current_signal": None,
                "last_trade": None,
                "last_trap": None,
                "recent_traps": [],
                "history": {
                    "bars_scanned": len(bars), "fake_breaks_detected": 0,
                    "traps_detected": 0, "orders_filled": 0, "closed_trades": 0,
                    "wins": 0, "losses": 0, "ambiguous_exit_bars": 0,
                    "pending_orders": 0, "active_trades": 0,
                    "win_rate_pct": None, "net_r": 0.0,
                },
                "reason": f"Need at least {minimum} closed candles to confirm the first {lookback}-left / {lookback}-right pivot.",
            }

        support: float | None = None
        resistance: float | None = None
        bear_trap_alert = False
        bull_trap_alert = False
        bear_alert_level: float | None = None
        bull_alert_level: float | None = None

        pending_order: dict[str, Any] | None = None
        open_trade: dict[str, Any] | None = None
        traps: list[dict[str, Any]] = []
        closed_trades: list[dict[str, Any]] = []
        fake_breaks = filled_count = ambiguous_count = wins = losses = 0
        net_r = 0.0

        for index, candle in enumerate(bars):
            # Resolve any position that was already open before this candle.
            if open_trade is not None and index > int(open_trade["fill_index"]):
                side = open_trade["side"]
                stop_hit = candle["l"] <= open_trade["stop_loss"] if side == "BUY" else candle["h"] >= open_trade["stop_loss"]
                target_hit = candle["h"] >= open_trade["take_profit"] if side == "BUY" else candle["l"] <= open_trade["take_profit"]
                if stop_hit or target_hit:
                    ambiguous = bool(stop_hit and target_hit)
                    if ambiguous:
                        ambiguous_count += 1
                    if stop_hit:
                        outcome = "SL HIT"
                        exit_price = float(open_trade["stop_loss"])
                        result_r = -1.0
                        losses += 1
                    else:
                        outcome = "TP HIT"
                        exit_price = float(open_trade["take_profit"])
                        risk = abs(float(open_trade["fill_price"]) - float(open_trade["stop_loss"]))
                        reward = abs(exit_price - float(open_trade["fill_price"]))
                        result_r = reward / risk if risk > 0 else float(rr_ratio)
                        wins += 1
                    open_trade["status"] = "CLOSED"
                    open_trade["outcome"] = outcome
                    open_trade["exit_price"] = exit_price
                    open_trade["exit_time"] = candle["t"]
                    open_trade["result_r"] = round(result_r, 4)
                    open_trade["ambiguous_exit_bar"] = ambiguous
                    closed_trades.append(open_trade)
                    net_r += result_r
                    open_trade = None

            # Confirm a pivot only after its full right-side lookback has closed.
            pivot_index = index - lookback
            if pivot_index >= lookback:
                left = bars[pivot_index - lookback:pivot_index]
                right = bars[pivot_index + 1:index + 1]
                pivot = bars[pivot_index]
                if len(left) == lookback and len(right) == lookback:
                    if pivot["h"] > max(item["h"] for item in left) and pivot["h"] >= max(item["h"] for item in right):
                        resistance = float(pivot["h"])
                    if pivot["l"] < min(item["l"] for item in left) and pivot["l"] <= min(item["l"] for item in right):
                        support = float(pivot["l"])

            previous_close = bars[index - 1]["c"] if index > 0 else None
            close = float(candle["c"])
            opened = float(candle["o"])

            # Match the uploaded Pine: trigger alerts on CLOSE cross, not wick-only excursions.
            if previous_close is not None and support is not None:
                if previous_close >= support and close < support:
                    bear_trap_alert = True
                    bear_alert_level = support
                    fake_breaks += 1
            if previous_close is not None and resistance is not None:
                if previous_close <= resistance and close > resistance:
                    bull_trap_alert = True
                    bull_alert_level = resistance
                    fake_breaks += 1

            # A reclaim bar creates a stop-entry order; it cannot fill on this same bar.
            buy_condition = bear_trap_alert and support is not None and close > support and close > opened
            if buy_condition:
                if pending_order is not None and pending_order.get("status") == "ENTRY PENDING":
                    pending_order["status"] = "SUPERSEDED"
                    pending_order["superseded_time"] = candle["t"]
                trigger = float(candle["h"])
                stop = float(candle["l"])
                risk = trigger - stop
                signal = {
                    "timeframe": timeframe, "trap_type": "BEAR TRAP", "side": "BUY",
                    "sweep_side": "SELL-SIDE / SUPPORT",
                    "break_level": bear_alert_level, "reclaim_level": support,
                    "signal_time": candle["t"], "signal_index": index,
                    "reclaim_high": trigger, "reclaim_low": stop,
                    "entry_trigger": trigger, "stop_loss": stop,
                    "take_profit": trigger + risk * float(rr_ratio),
                    "risk_per_unit": risk, "rr_ratio": float(rr_ratio),
                    "entry_order": "BUY STOP",
                    "status": "ENTRY PENDING" if risk > 0 else "INVALID RISK RANGE",
                    "outcome": None,
                    "eligible_from_index": index + (2 if open_trade is not None else 1),
                }
                traps.append(signal)
                pending_order = signal if risk > 0 else None
                bear_trap_alert = False
                bear_alert_level = None

            sell_condition = bull_trap_alert and resistance is not None and close < resistance and close < opened
            if sell_condition:
                if pending_order is not None and pending_order.get("status") == "ENTRY PENDING":
                    pending_order["status"] = "SUPERSEDED"
                    pending_order["superseded_time"] = candle["t"]
                trigger = float(candle["l"])
                stop = float(candle["h"])
                risk = stop - trigger
                signal = {
                    "timeframe": timeframe, "trap_type": "BULL TRAP", "side": "SELL",
                    "sweep_side": "BUY-SIDE / RESISTANCE",
                    "break_level": bull_alert_level, "reclaim_level": resistance,
                    "signal_time": candle["t"], "signal_index": index,
                    "reclaim_high": stop, "reclaim_low": trigger,
                    "entry_trigger": trigger, "stop_loss": stop,
                    "take_profit": trigger - risk * float(rr_ratio),
                    "risk_per_unit": risk, "rr_ratio": float(rr_ratio),
                    "entry_order": "SELL STOP",
                    "status": "ENTRY PENDING" if risk > 0 else "INVALID RISK RANGE",
                    "outcome": None,
                    "eligible_from_index": index + (2 if open_trade is not None else 1),
                }
                traps.append(signal)
                pending_order = signal if risk > 0 else None
                bull_trap_alert = False
                bull_alert_level = None

            # Pine has no timeout for a stop order: it remains until filled or replaced.
            if open_trade is None and pending_order is not None:
                if index >= int(pending_order["eligible_from_index"]):
                    side = pending_order["side"]
                    trigger = float(pending_order["entry_trigger"])
                    if side == "BUY" and float(candle["h"]) >= trigger:
                        fill_price = max(float(candle["o"]), trigger)
                    elif side == "SELL" and float(candle["l"]) <= trigger:
                        fill_price = min(float(candle["o"]), trigger)
                    else:
                        fill_price = None

                    if fill_price is not None:
                        stop = float(pending_order["stop_loss"])
                        fill_risk = abs(fill_price - stop)
                        if fill_risk > 0:
                            pending_order["status"] = "TRADE ACTIVE"
                            pending_order["fill_price"] = fill_price
                            pending_order["fill_time"] = candle["t"]
                            pending_order["fill_index"] = index
                            pending_order["actual_risk_per_unit"] = fill_risk
                            open_trade = pending_order
                            filled_count += 1
                            pending_order = None
                        else:
                            pending_order["status"] = "INVALID FILL RISK"
                            pending_order["fill_price"] = fill_price
                            pending_order["fill_time"] = candle["t"]
                            pending_order = None

        current_signal = open_trade if open_trade is not None else pending_order
        last_trade = closed_trades[-1] if closed_trades else None
        last_trap = traps[-1] if traps else None
        closed_count = len(closed_trades)
        summary = {
            "bars_scanned": len(bars),
            "fake_breaks_detected": fake_breaks,
            "traps_detected": len(traps),
            "orders_filled": filled_count,
            "closed_trades": closed_count,
            "wins": wins,
            "losses": losses,
            "ambiguous_exit_bars": ambiguous_count,
            "pending_orders": 1 if pending_order is not None else 0,
            "active_trades": 1 if open_trade is not None else 0,
            "win_rate_pct": round(wins / closed_count * 100.0, 2) if closed_count else None,
            "net_r": round(net_r, 4),
        }
        recent = [{
            "trap_type": signal["trap_type"], "side": signal["side"],
            "status": signal["status"], "outcome": signal.get("outcome"),
            "signal_time": signal["signal_time"], "entry_trigger": signal["entry_trigger"],
            "stop_loss": signal["stop_loss"], "take_profit": signal["take_profit"],
            "fill_price": signal.get("fill_price"), "exit_price": signal.get("exit_price"),
            "result_r": signal.get("result_r"),
        } for signal in traps[-10:]]

        if open_trade is not None:
            status = "TRADE ACTIVE"
            reason = "The reclaim-candle stop-entry was triggered in the historical replay; the source SL and single 1:2 TP are active."
        elif pending_order is not None:
            status = "ENTRY PENDING"
            reason = "The reclaim candle placed a stop order at its high/low. It remains pending until triggered or replaced, matching the Pine source."
        elif last_trade is not None:
            status = "LAST TRADE CLOSED"
            reason = f"The latest historical trade closed at {last_trade['outcome']}; no order is currently active."
        elif last_trap is not None:
            status = "LAST SETUP SUPERSEDED"
            reason = "A historical trap reclaim was found, but its pending order was replaced by a later setup."
        elif bear_trap_alert or bull_trap_alert:
            status = "FAKE BREAK ALERT"
            reason = "A close-based support/resistance break was detected; the reclaim candle has not formed yet."
        else:
            status = "WAITING"
            reason = "No active trap order. All available closed candles were replayed for the source strategy's pivot, fake-break and reclaim rules."

        return {
            "strategy_name": "Abhay Patil - Black Box (Buy & Sell)",
            "timeframe": timeframe,
            "status": status,
            "lookback": lookback,
            "rr_ratio": float(rr_ratio),
            "bars_scanned": len(bars),
            "first_bar_time": bars[0]["t"] if bars else None,
            "last_bar_time": bars[-1]["t"] if bars else None,
            "support_level": support,
            "resistance_level": resistance,
            "bear_trap_alert": bear_trap_alert,
            "bull_trap_alert": bull_trap_alert,
            "current_signal": cls._public_signal(current_signal, active=open_trade is not None),
            "last_trade": cls._public_signal(last_trade, active=False),
            "last_trap": cls._public_signal(last_trap, active=False),
            "recent_traps": recent,
            "history": summary,
            "reason": reason,
            "simulation_note": "Full available closed-OHLC replay. Stop orders can fill only on a later candle. If SL and TP are both touched in one candle, SL is counted first. Historical simulation is not a guaranteed live fill.",
        }

    def snapshot(self, candles: dict[str, Any]) -> dict[str, Any]:
        cleaned = {tf: self._clean_bars(candles.get(tf, [])) for tf in self.TIMEFRAMES}
        signature = tuple(
            (tf, len(rows), rows[0].get("t") if rows else None,
             rows[-1].get("t") if rows else None,
             rows[-1].get("c") if rows else None)
            for tf, rows in cleaned.items()
        )
        if signature == self._cache_signature and self._cache_result is not None:
            return copy.deepcopy(self._cache_result)
        result = {
            "strategy_name": "Abhay Patil - Black Box (Buy & Sell)",
            "lookback": self.LOOKBACK,
            "rr_ratio": self.RR_RATIO,
            "timeframes": {
                tf: self.replay_timeframe(tf, cleaned[tf])
                for tf in self.TIMEFRAMES
            },
        }
        self._cache_signature = signature
        self._cache_result = copy.deepcopy(result)
        return result
