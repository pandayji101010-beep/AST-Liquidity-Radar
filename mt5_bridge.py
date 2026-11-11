"""Read-only Vantage MT5 tick bridge for AST Liquidity Radar.

Run this on the same Windows PC as the logged-in Vantage MetaTrader 5 terminal.
It forwards quote ticks only; it does not send, modify, or close trades.
"""
from __future__ import annotations

import os
import sys
import time
import logging
from typing import Any

import requests
import MetaTrader5 as mt5
from dotenv import load_dotenv

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s | %(levelname)s | AST-MT5-Bridge | %(message)s",
)
log = logging.getLogger("AST-MT5-Bridge")

BACKEND_URL = os.getenv("AST_BACKEND_URL", "http://127.0.0.1:8000").rstrip("/")
BRIDGE_SECRET = os.getenv("VANTAGE_BRIDGE_SECRET", "replace-this-secret")
MT5_PATH = os.getenv("MT5_PATH", "").strip()
BTC_SYMBOL = os.getenv("MT5_BTC_SYMBOL", "BTCUSD").strip()
GOLD_SYMBOL = os.getenv("MT5_GOLD_SYMBOL", "XAUUSD").strip()
PRICE_SIDE = os.getenv("MT5_PRICE_SIDE", "bid").strip().lower()
POLL_SECONDS = max(0.25, float(os.getenv("MT5_POLL_SECONDS", "0.5")))


def initialise_mt5() -> None:
    if MT5_PATH:
        ok = mt5.initialize(path=MT5_PATH, timeout=60000)
    else:
        ok = mt5.initialize(timeout=60000)
    if not ok:
        raise RuntimeError(f"MetaTrader5 initialize failed: {mt5.last_error()}")


def select_symbol(preferred: str, asset: str) -> str:
    info = mt5.symbol_info(preferred)
    if info is not None:
        if not info.visible and not mt5.symbol_select(preferred, True):
            raise RuntimeError(f"Could not select MT5 symbol {preferred}: {mt5.last_error()}")
        log.info("Mapped %s to Vantage MT5 symbol: %s", asset, preferred)
        return preferred

    all_symbols = mt5.symbols_get() or []
    token = "BTC" if asset == "BTC" else "XAU"
    matches = [s.name for s in all_symbols if token in s.name.upper()]
    preview = ", ".join(matches[:30]) if matches else "No matching symbols returned"
    raise RuntimeError(
        f"MT5 symbol '{preferred}' not found for {asset}. "
        f"Open Market Watch and check exact broker symbol. Matching symbols: {preview}. "
        f"Then set MT5_{asset}_SYMBOL in .env."
    )


def tick_payload(asset: str, symbol: str, tick: Any) -> dict[str, Any] | None:
    bid = float(getattr(tick, "bid", 0.0) or 0.0)
    ask = float(getattr(tick, "ask", 0.0) or 0.0)
    last = float(getattr(tick, "last", 0.0) or 0.0)
    if bid <= 0 and ask <= 0 and last <= 0:
        return None

    mid = (bid + ask) / 2.0 if bid > 0 and ask > 0 else 0.0
    choices = {
        "bid": bid,
        "mid": mid,
        "last": last,
        "ask": ask,
    }
    price = choices.get(PRICE_SIDE, bid or mid or last or ask)
    if price <= 0:
        price = bid or mid or last or ask
    if price <= 0:
        return None

    tick_time = int(getattr(tick, "time_msc", 0) or 0)
    if tick_time <= 0:
        tick_time = int(getattr(tick, "time", time.time()) * 1000)

    return {
        "asset": asset,
        "symbol": symbol,
        "time_msc": tick_time,
        "bid": bid or None,
        "ask": ask or None,
        "last": last or None,
        "price": price,
        "price_side": PRICE_SIDE,
    }


def main() -> int:
    if not BRIDGE_SECRET or BRIDGE_SECRET == "replace-this-secret":
        log.error("Set VANTAGE_BRIDGE_SECRET in .env to the same value as the backend.")
        return 2

    try:
        initialise_mt5()
        # GOLD is the requested broker quote. BTC is optional: some Vantage
        # account types do not expose a crypto CFD symbol.
        gold_symbol = select_symbol(GOLD_SYMBOL, "GOLD")
        btc_symbol = None
        try:
            btc_symbol = select_symbol(BTC_SYMBOL, "BTC")
        except Exception as exc:
            log.warning("BTC MT5 quote disabled; GOLD bridge will continue: %s", exc)
    except Exception as exc:
        log.error("MT5 startup failed: %s", exc)
        mt5.shutdown()
        return 2

    account = mt5.account_info()
    terminal = mt5.terminal_info()
    log.info("MT5 terminal connected: %s", bool(terminal and terminal.connected))
    log.info("Vantage server: %s", getattr(account, "server", "unknown") if account else "not signed in")
    log.info("Forwarding Vantage quote ticks to %s (read-only)", BACKEND_URL)
    log.info("Price side: %s | poll: %.2fs", PRICE_SIDE, POLL_SECONDS)

    session = requests.Session()
    url = f"{BACKEND_URL}/api/vantage/mt5"
    headers = {"X-AST-Bridge-Secret": BRIDGE_SECRET}
    symbols = ([("BTC", btc_symbol)] if btc_symbol else []) + [("GOLD", gold_symbol)]
    last_sent: dict[str, int] = {}
    last_health_check = 0.0

    try:
        while True:
            now = time.time()
            if now - last_health_check > 30:
                try:
                    health = session.get(f"{BACKEND_URL}/api/health", timeout=3)
                    health.raise_for_status()
                except requests.RequestException as exc:
                    log.warning("Backend is not reachable yet: %s", exc)
                last_health_check = now

            for asset, symbol in symbols:
                tick = mt5.symbol_info_tick(symbol)
                if tick is None:
                    log.warning("No tick for %s (%s): %s", asset, symbol, mt5.last_error())
                    continue
                payload = tick_payload(asset, symbol, tick)
                if payload is None:
                    continue
                stamp = int(payload["time_msc"])
                if last_sent.get(asset) == stamp:
                    continue
                try:
                    response = session.post(url, headers=headers, json=payload, timeout=3)
                    response.raise_for_status()
                    result = response.json()
                    last_sent[asset] = stamp
                    if result.get("matched"):
                        log.info(
                            "%s %s | Vantage %.5f | Binance %.5f | ratio %s | samples %s",
                            asset,
                            symbol,
                            payload["price"],
                            result.get("matched_exchange_price") or 0.0,
                            result.get("basis_ratio"),
                            result.get("basis_samples"),
                        )
                    else:
                        log.debug("%s tick received; waiting for matched Binance timestamp", asset)
                except requests.RequestException as exc:
                    log.warning("Could not forward %s tick: %s", asset, exc)
            time.sleep(POLL_SECONDS)

    except KeyboardInterrupt:
        log.info("Stopping Vantage bridge")
    finally:
        session.close()
        mt5.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
