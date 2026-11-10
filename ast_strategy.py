
from collections import defaultdict


class ASTStructureStrategy:
    """Multi-timeframe structure, liquidity and setup scanner."""

    TIMEFRAMES = ("1d", "4h", "1h", "30m", "15m", "5m", "1m")

    def __init__(self):
        self.direction_lock = None
        self.last_events = []

    @staticmethod
    def _number(candle, key):
        try:
            return float(candle[key])
        except (KeyError, TypeError, ValueError):
            return None

    def _analyse_tf(self, tf, candles):
        candles = list(candles or [])
        if len(candles) < 30:
            return {
                "timeframe": tf,
                "status": "WARMING UP",
                "bars": len(candles),
                "bos": None,
                "choch": None,
                "trend": "NEUTRAL",
                "sweep": None,
                "zone": None,
            }

        bars = []
        for c in candles:
            values = {
                "o": self._number(c, "o"),
                "h": self._number(c, "h"),
                "l": self._number(c, "l"),
                "c": self._number(c, "c"),
                "t": c.get("t"),
            }
            if all(values[k] is not None for k in ("o", "h", "l", "c")):
                bars.append(values)

        if len(bars) < 30:
            return {
                "timeframe": tf, "status": "INVALID DATA",
                "bars": len(bars), "bos": None, "choch": None,
                "trend": "NEUTRAL", "sweep": None, "zone": None,
            }

        last = bars[-1]
        previous = bars[-21:-1]
        prior_high = max(x["h"] for x in previous)
        prior_low = min(x["l"] for x in previous)

        # Confirmed historical swing levels; no future candles are used.
        swing_highs = []
        swing_lows = []

        for i in range(3, len(bars) - 3):
            window = bars[i - 3:i + 4]
            if bars[i]["h"] == max(x["h"] for x in window):
                swing_highs.append(bars[i]["h"])
            if bars[i]["l"] == min(x["l"] for x in window):
                swing_lows.append(bars[i]["l"])

        swing_high = swing_highs[-1] if swing_highs else prior_high
        swing_low = swing_lows[-1] if swing_lows else prior_low

        bull_bos = last["c"] > swing_high
        bear_bos = last["c"] < swing_low

        body = abs(last["c"] - last["o"])
        avg_range = sum(x["h"] - x["l"] for x in bars[-15:-1]) / 14
        displacement = avg_range > 0 and body >= avg_range * 1.5

        bull_sweep = last["l"] < prior_low and last["c"] > prior_low
        bear_sweep = last["h"] > prior_high and last["c"] < prior_high

        if bull_bos:
            trend = "BULLISH"
        elif bear_bos:
            trend = "BEARISH"
        elif last["c"] > swing_low and last["c"] >= last["o"]:
            trend = "BULLISH"
        elif last["c"] < swing_high and last["c"] <= last["o"]:
            trend = "BEARISH"
        else:
            trend = "NEUTRAL"

        sweep = (
            "LOWER_SWEEP" if bull_sweep else
            "UPPER_SWEEP" if bear_sweep else None
        )

        zone = None
        if displacement and last["c"] > last["o"]:
            zone = {
                "side": "BUY",
                "low": last["l"],
                "high": last["o"],
                "timeframe": tf,
                "retests": 0,
            }
        elif displacement and last["c"] < last["o"]:
            zone = {
                "side": "SELL",
                "low": last["o"],
                "high": last["h"],
                "timeframe": tf,
                "retests": 0,
            }

        return {
            "timeframe": tf,
            "status": "READY",
            "bars": len(bars),
            "trend": trend,
            "bos": "BULLISH" if bull_bos else "BEARISH" if bear_bos else None,
            "choch": None,
            "sweep": sweep,
            "displacement": displacement,
            "swing_high": swing_high,
            "swing_low": swing_low,
            "equal_high": (
                (swing_highs[-1] + swing_highs[-2]) / 2
                if len(swing_highs) >= 2
                and abs(swing_highs[-1] - swing_highs[-2])
                <= avg_range * 0.2 else None
            ),
            "equal_low": (
                (swing_lows[-1] + swing_lows[-2]) / 2
                if len(swing_lows) >= 2
                and abs(swing_lows[-1] - swing_lows[-2])
                <= avg_range * 0.2 else None
            ),
            "zone": zone,
            "last_close": last["c"],
            "candle_time": last["t"],
        }

    def snapshot(self, candles):
        timeframes = {}
        zones = []
        liquidity = []
        events = []
        setups = []

        for tf in self.TIMEFRAMES:
            result = self._analyse_tf(tf, candles.get(tf, []))
            timeframes[tf] = result

            if result.get("zone"):
                zones.append(result["zone"])

            if result.get("swing_high") is not None:
                liquidity.append({
                    "timeframe": tf,
                    "side": "BUY_SIDE",
                    "price": result["swing_high"],
                    "type": "SWING_HIGH",
                })

            if result.get("swing_low") is not None:
                liquidity.append({
                    "timeframe": tf,
                    "side": "SELL_SIDE",
                    "price": result["swing_low"],
                    "type": "SWING_LOW",
                })

            if result.get("sweep"):
                events.append({
                    "timeframe": tf,
                    "type": result["sweep"],
                    "price": result.get("last_close"),
                })

            side = None
            checks = {
                "higher_tf_consensus": False,
                "liquidity_sweep": bool(result.get("sweep")),
                "displacement": bool(result.get("displacement")),
                "structure_break": bool(result.get("bos")),
            }

            if result.get("sweep") == "LOWER_SWEEP":
                side = "BUY"
            elif result.get("sweep") == "UPPER_SWEEP":
                side = "SELL"

            confirmed = (
                side is not None
                and checks["displacement"]
                and (
                    (side == "BUY" and result.get("bos") == "BULLISH")
                    or (side == "SELL" and result.get("bos") == "BEARISH")
                )
            )

            setups.append({
                "entry_timeframe": tf,
                "status": (
                    "ENTRY CONFIRMED" if confirmed
                    else "SETUP FORMING" if side
                    else "WAITING"
                ),
                "side": side,
                "score": sum(bool(v) for v in checks.values()) * 25,
                "trend_lock": self.direction_lock or "NEUTRAL",
                "macro_buy": 0,
                "macro_sell": 0,
                "price": result.get("last_close"),
                "checks": checks,
                "reason": (
                    "Sweep + displacement + structure break confirmed"
                    if confirmed else
                    "Waiting for sweep, displacement and structure confirmation"
                ),
            })

        return {
            "direction_lock": self.direction_lock or "NEUTRAL",
            "timeframes": timeframes,
            "zones": zones,
            "liquidity": liquidity,
            "events": events,
            "setups": setups,
        }
