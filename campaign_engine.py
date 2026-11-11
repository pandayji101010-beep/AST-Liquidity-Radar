from __future__ import annotations

import math
import os
from typing import Any


def _float_env(name: str, default: float) -> float:
    try:
        value = float(os.getenv(name, str(default)))
        return value if math.isfinite(value) and value > 0 else default
    except (TypeError, ValueError):
        return default


class ASTCampaignEngine:
    """Owns quantity calculation, stop/target plans, exposure and campaign lifecycle."""

    def __init__(self, asset: str = "MARKET") -> None:
        self.asset = asset.upper()
        self.per_position_risk_pct = _float_env("AST_CAMPAIGN_ENTRY_RISK_PCT", 0.20)
        self.max_campaign_risk_pct = _float_env("AST_CAMPAIGN_MAX_RISK_PCT", 1.50)
        self.daily_loss_limit_pct = _float_env("AST_CAMPAIGN_DAILY_LOSS_PCT", 2.00)
        self.max_positions = max(1, int(os.getenv("AST_CAMPAIGN_MAX_POSITIONS", "10")))
        self.minimum_target_r = 1.50
        self.account_equity = _float_env("AST_CAMPAIGN_ACCOUNT_EQUITY", 0.0)
        self.contract_value_per_price_unit = _float_env(
            "AST_CAMPAIGN_BTC_VALUE_PER_PRICE_UNIT" if self.asset == "BTC"
            else "AST_CAMPAIGN_GOLD_VALUE_PER_PRICE_UNIT", 0.0
        )
        self.campaign: dict[str, Any] | None = None
        self.pending: dict[str, Any] | None = None
        self.daily_pnl_pct: dict[str, float] = {}
        self.closed: list[dict[str, Any]] = []
        self.campaigns_started = 0
        self.closed_campaign_entry_counts: list[int] = []
        self.last_rejection: str | None = None

    def reset(self) -> None:
        self.campaign = None
        self.pending = None
        self.daily_pnl_pct = {}
        self.closed = []
        self.campaigns_started = 0
        self.closed_campaign_entry_counts = []
        self.last_rejection = None

    @staticmethod
    def _day_pnl_key(timestamp_ms: int) -> str:
        from datetime import datetime, timezone
        return datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")

    def _open_positions(self) -> list[dict[str, Any]]:
        if not self.campaign:
            return []
        return [p for p in self.campaign.get("positions", []) if p.get("status") == "OPEN"]

    def _quantity(self, entry: float, stop: float) -> tuple[float | None, str]:
        if self.account_equity <= 0 or self.contract_value_per_price_unit <= 0:
            return None, "UNAVAILABLE · configure account equity and asset contract-value settings"
        risk_cash = self.account_equity * self.per_position_risk_pct / 100.0
        loss_per_unit = abs(entry - stop) * self.contract_value_per_price_unit
        if loss_per_unit <= 0:
            return None, "UNAVAILABLE · invalid stop distance"
        return risk_cash / loss_per_unit, "ESTIMATED · based on configured account and contract-value settings"

    def _position_risk_pct(self) -> float:
        positions = self._open_positions()
        total = 0.0
        for p in positions:
            original = float(p.get("initial_risk") or 0.0)
            remaining = float(p.get("remaining_fraction") or 0.0)
            entry, stop = float(p["entry_price"]), float(p["stop_loss"])
            if original <= 0:
                continue
            adverse_distance = max(entry - stop, 0.0) if p["side"] == "BUY" else max(stop - entry, 0.0)
            if p.get("quantity") is not None and self.account_equity > 0 and self.contract_value_per_price_unit > 0:
                cash_risk = adverse_distance * float(p["quantity"]) * self.contract_value_per_price_unit * remaining
                total += cash_risk / self.account_equity * 100.0
            else:
                total += float(p.get("risk_pct") or 0.0) * min(1.0, adverse_distance / original) * remaining
        return round(total, 4)

    def entry_context(self, micro: dict[str, Any] | None = None) -> dict[str, Any]:
        positions = self._open_positions()
        side = self.campaign.get("side") if self.campaign else None
        current_risk = self._position_risk_pct()
        protected = bool(positions) and all(
            (p["side"] == "BUY" and float(p["stop_loss"]) >= float(p["entry_price"]))
            or (p["side"] == "SELL" and float(p["stop_loss"]) <= float(p["entry_price"]))
            for p in positions
        )
        atr = float((micro or {}).get("atr") or 0.0)
        favorable = False
        if self.campaign and atr > 0:
            last_entry = float(self.campaign.get("last_entry_price") or 0.0)
            extreme = float(self.campaign.get("favorable_extreme") or last_entry)
            excursion = extreme - last_entry if side == "BUY" else last_entry - extreme
            favorable = excursion >= max(atr * 0.50, last_entry * 0.0002)
        can_add = bool(
            self.campaign and side in ("BUY", "SELL") and protected and favorable
            and len(positions) < self.max_positions
            and current_risk + self.per_position_risk_pct <= self.max_campaign_risk_pct
            and self.pending is None
        )
        return {
            "active": bool(self.campaign and positions),
            "side": side,
            "open_positions": len(positions),
            "risk_pct": current_risk,
            "max_risk_pct": self.max_campaign_risk_pct,
            "can_add_position": can_add,
            "existing_positions_protected": protected,
            "favorable_extension_confirmed": favorable,
        }

    def _record_closed(self, position: dict[str, Any]) -> None:
        self.closed.append({
            key: position.get(key) for key in (
                "id", "kind", "side", "entry_price", "stop_loss", "take_profit",
                "status", "result_r", "entry_time", "exit_time", "risk_pct",
            )
        })

    def _close_campaign(self) -> None:
        if self.campaign:
            self.closed_campaign_entry_counts.append(int(self.campaign.get("entries_count", 0)))
        self.campaign = None
        self.pending = None

    def _manage_bar(
        self,
        bar: dict[str, Any],
        close_time: int,
        micro: dict[str, Any],
        mtf: dict[str, Any],
        day_key: str,
    ) -> None:
        if not self.campaign:
            return
        side = self.campaign["side"]
        if mtf.get("status") == "CONFIRMED" and mtf.get("side") in ("BUY", "SELL") and mtf["side"] != side:
            for p in self._open_positions():
                move = (float(bar["c"]) - float(p["entry_price"])) * (1 if side == "BUY" else -1)
                r_now = move / float(p["initial_risk"]) if p.get("initial_risk") else 0.0
                result_r = r_now * float(p["remaining_fraction"]) + float(p["realized_r"])
                p.update({"status": "CAMPAIGN INVALIDATED", "exit_price": float(bar["c"]), "exit_time": close_time, "result_r": round(result_r, 3)})
                self.daily_pnl_pct[day_key] = self.daily_pnl_pct.get(day_key, 0.0) + float(p["risk_pct"]) * result_r
                self._record_closed(p)
            self._close_campaign()
            return

        kept = []
        for p in self._open_positions():
            pside = p["side"]
            stop = float(p["stop_loss"])
            target = float(p["take_profit"])
            stop_hit = float(bar["l"]) <= stop if pside == "BUY" else float(bar["h"]) >= stop
            target_hit = float(bar["h"]) >= target if pside == "BUY" else float(bar["l"]) <= target
            one_r = float(p["entry_price"]) + float(p["initial_risk"]) * (1 if pside == "BUY" else -1)
            partial_hit = float(bar["h"]) >= one_r if pside == "BUY" else float(bar["l"]) <= one_r
            if stop_hit:
                result_r = float(p["realized_r"])
                if float(p["remaining_fraction"]) > 0:
                    move = (stop - float(p["entry_price"])) * (1 if pside == "BUY" else -1)
                    result_r += (move / float(p["initial_risk"])) * float(p["remaining_fraction"])
                p.update({"status": "STOP LOSS", "exit_price": stop, "exit_time": close_time, "result_r": round(result_r, 3)})
            else:
                if partial_hit and not p["partial_taken"]:
                    p["realized_r"] += 0.5
                    p["remaining_fraction"] = 0.5
                    p["partial_taken"] = True
                    p["stop_loss"] = p["entry_price"]
                    p["management"] = "50% BOOKED AT 1R · STOP TO BREAKEVEN"
                if target_hit:
                    move = (target - float(p["entry_price"])) * (1 if pside == "BUY" else -1)
                    target_r = move / float(p["initial_risk"])
                    if p["remaining_fraction"] > 0:
                        p["realized_r"] += target_r * float(p["remaining_fraction"])
                    p["remaining_fraction"] = 0.0
                    p.update({"status": "FINAL TARGET HIT", "exit_price": target, "exit_time": close_time, "result_r": round(float(p["realized_r"]), 3)})
                elif p["partial_taken"]:
                    swing = micro.get("swing_low") if pside == "BUY" else micro.get("swing_high")
                    atr = float(micro.get("atr") or 0.0)
                    if isinstance(swing, (int, float)) and atr > 0:
                        candidate_stop = float(swing) - atr * 0.10 if pside == "BUY" else float(swing) + atr * 0.10
                        if pside == "BUY" and float(p["entry_price"]) <= candidate_stop < float(bar["c"]):
                            p["stop_loss"] = max(float(p["stop_loss"]), candidate_stop)
                        elif pside == "SELL" and float(bar["c"]) < candidate_stop <= float(p["entry_price"]):
                            p["stop_loss"] = min(float(p["stop_loss"]), candidate_stop)
                    p["management"] = "PARTIAL BOOKED · STRUCTURE TRAILING"
            if p["status"] == "OPEN":
                kept.append(p)
            else:
                result_r = float(p.get("result_r") or 0.0)
                self.daily_pnl_pct[day_key] = self.daily_pnl_pct.get(day_key, 0.0) + float(p["risk_pct"]) * result_r
                self._record_closed(p)
        self.campaign["positions"] = kept
        if not kept:
            self._close_campaign()

    def _fill_pending(self, bar: dict[str, Any], close_time: int, day_key: str) -> dict[str, Any] | None:
        if self.pending is None:
            return None
        plan = self.pending
        self.pending = None
        fill = float(bar["o"])
        gap = abs(fill - float(plan["signal_price"]))
        gap_limit = max(float(plan.get("atr") or plan["initial_risk"]) * 0.50, float(plan["signal_price"]) * 0.0002)
        if gap > gap_limit:
            self.last_rejection = "Next 1m open gapped too far from the confirmed signal price."
            return None
        side = plan["side"]
        stop, target = float(plan["stop_loss"]), float(plan["take_profit"])
        risk = abs(fill - stop)
        rr = ((target - fill) / risk) if side == "BUY" and risk > 0 else ((fill - target) / risk) if side == "SELL" and risk > 0 else 0.0
        if risk <= 0 or rr < self.minimum_target_r:
            self.last_rejection = "Next-open fill invalidated the planned stop/target reward-to-risk."
            return None
        context = self.entry_context({"atr": plan.get("atr")})
        if plan["kind"] == "MAIN" and self.campaign is not None:
            self.last_rejection = "Main entry rejected because another campaign is already open."
            return None
        if plan["kind"] == "ADD-ON" and (not context["active"] or context["side"] != side or not context["can_add_position"]):
            self.last_rejection = "Add-on no longer passes protection, extension or campaign-risk gates at fill."
            return None
        if context["risk_pct"] + self.per_position_risk_pct > self.max_campaign_risk_pct:
            self.last_rejection = "Total campaign risk cap blocks the next-open fill."
            return None
        if self.daily_pnl_pct.get(day_key, 0.0) <= -self.daily_loss_limit_pct:
            self.last_rejection = "Daily loss limit blocks the next-open fill."
            return None
        quantity, quantity_status = self._quantity(fill, stop)
        if self.campaign is None:
            self.campaigns_started += 1
            self.campaign = {
                "id": f"{self.asset}-{close_time}", "side": side, "started_at": close_time,
                "positions": [], "last_entry_price": fill, "favorable_extreme": fill,
                "entries_count": 0,
            }
        pos = {
            "id": f"{self.asset}-{plan['signal_time']}", "kind": plan["kind"], "side": side,
            "timeframe": "1m", "signal_time": plan["signal_time"], "entry_time": close_time,
            "entry_price": fill, "planned_entry": plan["signal_price"],
            "stop_loss": stop, "initial_stop_loss": stop, "take_profit": target,
            "partial_take_profit": fill + risk * (1 if side == "BUY" else -1),
            "initial_risk": risk, "risk_pct": self.per_position_risk_pct,
            "quantity": quantity, "quantity_status": quantity_status,
            "target_r": round(rr, 2), "target_source": plan.get("target_source"),
            "remaining_fraction": 1.0, "partial_taken": False, "realized_r": 0.0,
            "result_r": None, "status": "OPEN", "score": plan.get("score"),
            "grade": plan.get("grade"), "reason": plan.get("reason"),
            "management": "INITIAL SL ACTIVE",
        }
        self.campaign["positions"].append(pos)
        self.campaign["entries_count"] += 1
        self.campaign["last_entry_price"] = fill
        self.campaign["favorable_extreme"] = fill
        return pos

    def process_bar(
        self,
        bar: dict[str, Any],
        close_time: int,
        micro: dict[str, Any],
        mtf: dict[str, Any],
    ) -> dict[str, Any] | None:
        day_key = self._day_pnl_key(int(bar["t"]))
        if self.pending is not None and (mtf.get("status") != "CONFIRMED" or mtf.get("side") != self.pending.get("side")):
            self.pending = None
            self.last_rejection = "Pending entry canceled because MTF direction is no longer confirmed at fill time."
        self._manage_bar(bar, close_time, micro, mtf, day_key)
        filled = self._fill_pending(bar, close_time, day_key)
        if self.campaign and self.campaign.get("positions"):
            side = self.campaign["side"]
            if side == "BUY":
                self.campaign["favorable_extreme"] = max(float(self.campaign["favorable_extreme"]), float(bar["h"]))
            else:
                self.campaign["favorable_extreme"] = min(float(self.campaign["favorable_extreme"]), float(bar["l"]))
        return filled

    def queue_candidate(self, candidate: dict[str, Any]) -> dict[str, Any]:
        day_key = self._day_pnl_key(int(candidate["signal_time"]))
        return self._queue_candidate(candidate, day_key)

    def _queue_candidate(self, candidate: dict[str, Any], day_key: str) -> dict[str, Any]:
        self.last_rejection = None
        context = self.entry_context({"atr": candidate.get("atr")})
        if self.pending is not None:
            return {"accepted": False, "reason": "An entry is already pending next 1m open."}
        if self.daily_pnl_pct.get(day_key, 0.0) <= -self.daily_loss_limit_pct:
            return {"accepted": False, "reason": "Daily loss limit reached."}
        side, kind = candidate.get("side"), candidate.get("kind")
        if kind == "MAIN" and context["active"]:
            return {"accepted": False, "reason": "Campaign already active; main entry cannot replace it."}
        if kind == "ADD-ON" and not (context["active"] and context["side"] == side and context["can_add_position"]):
            return {"accepted": False, "reason": "Add-on requires same-side campaign, protected open positions and a fresh favorable extension."}
        if context["risk_pct"] + self.per_position_risk_pct > self.max_campaign_risk_pct:
            return {"accepted": False, "reason": "Maximum total campaign risk would be exceeded."}
        plan = self._build_order_plan(candidate)
        if not plan:
            return {"accepted": False, "reason": self.last_rejection or "Campaign Engine could not build a valid risk plan."}
        plan.update({
            "status": "ENTRY READY · NEXT 1M OPEN",
            "execution": "Signal formed on closed 1m candle; fill is simulated at next 1m open.",
        })
        self.pending = plan
        return {"accepted": True, "plan": dict(plan)}

    def _build_order_plan(self, candidate: dict[str, Any]) -> dict[str, Any] | None:
        side = candidate.get("side")
        entry = float(candidate.get("signal_price") or 0.0)
        sweep = float(candidate.get("sweep_price") or 0.0)
        atr = float(candidate.get("atr") or 0.0)
        if side not in ("BUY", "SELL") or entry <= 0 or sweep <= 0 or atr <= 0:
            self.last_rejection = "Entry candidate missing valid price, side, sweep anchor or ATR."
            return None
        stop = sweep - atr * 0.12 if side == "BUY" else sweep + atr * 0.12
        if (side == "BUY" and stop >= entry) or (side == "SELL" and stop <= entry):
            self.last_rejection = "Stop loss is on the wrong side of the proposed entry."
            return None
        risk = abs(entry - stop)
        if risk <= 0:
            self.last_rejection = "Invalid stop distance."
            return None
        eligible = []
        for item in candidate.get("liquidity_targets", []):
            target = float(item.get("price") or 0.0)
            rr = (target - entry) / risk if side == "BUY" else (entry - target) / risk
            if target > entry and side == "BUY" and rr >= self.minimum_target_r or target < entry and side == "SELL" and rr >= self.minimum_target_r:
                eligible.append((abs(target - entry), target, rr, item))
        if not eligible:
            self.last_rejection = "No directional liquidity target meets minimum 1.5R; no arbitrary TP will be invented."
            return None
        _, target, rr, target_info = min(eligible, key=lambda item: item[0])
        quantity, quantity_status = self._quantity(entry, stop)
        return {
            "asset": self.asset, "side": side, "kind": candidate["kind"], "signal_time": candidate["signal_time"],
            "signal_close": entry, "entry_price": entry, "stop_loss": stop,
            "partial_take_profit": entry + risk * (1 if side == "BUY" else -1),
            "take_profit": target, "target_r": round(rr, 2),
            "risk_pct": self.per_position_risk_pct,
            "quantity": quantity, "quantity_status": quantity_status,
            "target_source": target_info, "score": candidate.get("score"), "grade": candidate.get("grade"),
            "score_checks": candidate.get("score_checks"), "entry_zone": candidate.get("zone"),
            "market_context_score": candidate.get("market_context_score"),
            "htf_alignment": candidate.get("htf_agreement_pct"), "atr": atr,
            "reason": candidate.get("reason"),
        }

    def snapshot(self) -> dict[str, Any]:
        positions = self._open_positions()
        risk = self._position_risk_pct()
        context = self.entry_context()
        campaign = {
            "status": "ACTIVE" if self.campaign and positions else "NO CAMPAIGN",
            "id": self.campaign.get("id") if self.campaign else None,
            "side": self.campaign.get("side") if self.campaign else None,
            "started_at": self.campaign.get("started_at") if self.campaign else None,
            "positions": positions,
            "open_positions": len(positions),
            "entries_count": self.campaign.get("entries_count", 0) if self.campaign else 0,
            "risk_pct": risk,
            "max_risk_pct": self.max_campaign_risk_pct,
            "per_position_risk_pct": self.per_position_risk_pct,
            "max_positions": self.max_positions,
            "can_add_position": context["can_add_position"],
            "daily_loss_limit_pct": self.daily_loss_limit_pct,
            "existing_positions_protected": context["existing_positions_protected"],
            "favorable_extension_confirmed": context["favorable_extension_confirmed"],
        }
        closed = self.closed
        wins = sum(1 for x in closed if float(x.get("result_r") or 0.0) > 0)
        losses = len(closed) - wins
        gross_win = sum(max(float(x.get("result_r") or 0.0), 0.0) for x in closed)
        gross_loss = sum(abs(min(float(x.get("result_r") or 0.0), 0.0)) for x in closed)
        cumulative = peak = max_dd = 0.0
        consecutive = max_consecutive = 0
        holds = []
        for item in closed:
            result_r = float(item.get("result_r") or 0.0)
            cumulative += float(item.get("risk_pct") or 0.0) * result_r
            peak = max(peak, cumulative)
            max_dd = max(max_dd, peak - cumulative)
            if result_r > 0:
                consecutive = 0
            else:
                consecutive += 1
                max_consecutive = max(max_consecutive, consecutive)
            if item.get("entry_time") and item.get("exit_time"):
                holds.append(max(0.0, (int(item["exit_time"]) - int(item["entry_time"])) / 60_000.0))
        performance = {
            "closed_positions": len(closed), "wins": wins, "losses": losses,
            "win_rate_pct": round(wins / len(closed) * 100.0, 2) if closed else None,
            "profit_factor": round(gross_win / gross_loss, 3) if gross_loss > 0 else None,
            "gross_win_r": round(gross_win, 3), "gross_loss_r": round(gross_loss, 3),
            "net_r": round(sum(float(x.get("result_r") or 0.0) for x in closed), 3),
            "max_drawdown_pct_of_equity": round(max_dd, 3),
            "max_consecutive_losses": max_consecutive,
            "average_holding_minutes": round(sum(holds) / len(holds), 1) if holds else None,
            "campaigns_started": self.campaigns_started,
            "average_entries_per_closed_campaign": round(sum(self.closed_campaign_entry_counts) / len(self.closed_campaign_entry_counts), 2) if self.closed_campaign_entry_counts else None,
            "sample_scope": "Sequential replay of loaded closed 1m candles.",
            "fees_slippage_included": False,
        }
        return {
            "campaign": campaign,
            "pending": dict(self.pending) if self.pending else None,
            "recent_closed": closed[-10:],
            "performance": performance,
            "last_rejection": self.last_rejection,
        }
