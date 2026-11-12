# AST Liquidity Radar

## Historical data sources

- BTC uses Binance USD-M Futures public archives and the live Binance Futures stream.
- BTC historical OHLC is loaded from native Binance USD-M archives independently for 1m, 5m, 15m, 30m, 1h, 4h and 1d. Up to 25,000 closed candles per timeframe are retained; 1m resampling is only a fallback when a native interval archive is unavailable.
- GOLD uses Yahoo Finance COMEX continuous gold futures (GC=F) as a fallback reference until the Windows MT5 bridge supplies native Vantage XAUUSD broker candles. Once connected, GOLD structure and levels switch to the direct broker OHLC feed.
- The Vantage MT5 bridge forwards broker bid/ask/last ticks and native GOLD OHLC candles to the dashboard. It is read-only; it does not place or manage trades.

## Enable Vantage XAUUSD live quote mapping

The Vantage MT5 bridge cannot run inside the Render web service or on GitHub Actions. It must run on a Windows PC or Windows VPS where the Vantage MetaTrader 5 desktop terminal is installed and logged in.

### 1. Configure the Render secret

1. Generate a strong random secret on the Windows PC:
   \`python -c "import secrets; print(secrets.token_urlsafe(32))"\`
2. Open the Render web service for AST Liquidity Radar.
3. Go to **Environment** and add:
   - Key: \`VANTAGE_BRIDGE_SECRET\`
   - Value: the generated secret
4. Save the environment change and let Render redeploy.
5. Do not put this value in GitHub, a committed file, or a screenshot.

### 2. Prepare the Windows MT5 terminal

1. Install the Vantage MetaTrader 5 desktop terminal and sign in to the intended Vantage account (demo or live).
2. Open **Market Watch** and make sure the broker's XAUUSD instrument is visible and receiving quotes.
3. Check the exact broker symbol. It may be \`XAUUSD\`, \`XAUUSD.\`, \`XAUUSDm\`, or another account-specific suffix. Use the exact spelling shown by the terminal.
4. Keep the terminal logged in and connected.

### 3. Install and configure the local bridge

From a terminal in the repository folder, run:

\`\`\`powershell
py -m pip install -r requirements-bridge.txt
\`\`\`

Create a local \`.env\` file in the repository folder. Do not commit or upload it:

\`\`\`dotenv
AST_BACKEND_URL=https://ast-liquidity-radar.onrender.com
VANTAGE_BRIDGE_SECRET=PASTE_THE_SAME_SECRET_USED_IN_RENDER
MT5_GOLD_SYMBOL=XAUUSD
MT5_BTC_SYMBOL=BTCUSD
MT5_PRICE_SIDE=bid
MT5_POLL_SECONDS=0.5
MT5_GOLD_CANDLE_SYNC_SECONDS=5
\`\`\`

Replace \`MT5_GOLD_SYMBOL\` with the exact symbol visible in Vantage Market Watch. Replace \`MT5_BTC_SYMBOL\` if your account exposes BTC under another name; BTC is optional, so a missing BTC symbol does not stop the GOLD bridge.

Start the read-only quote forwarder:

\`\`\`powershell
py mt5_bridge.py
\`\`\`

Keep this process and the MT5 terminal running. For continuous 24/7 quotes, use a Windows VPS with MT5 kept connected.

### 4. Verify the connection

Open:

- \`https://ast-liquidity-radar.onrender.com/api/health\`
- \`https://ast-liquidity-radar.onrender.com/api/snapshot?timeframe=5m\`

Health should report \`vantage_mt5_bridge_secret_configured: true\`. In the snapshot, check \`vantage_feed_fresh\`, \`vantage_age_seconds\`, and \`basis_samples\`. The dashboard quote mapping becomes available after fresh ticks are received and enough timestamp-matched samples have been collected.

### Troubleshooting

- **Invalid bridge secret / HTTP 401:** the local \`.env\` secret and Render \`VANTAGE_BRIDGE_SECRET\` must match exactly.
- **HTTP 503:** set the Render environment variable and wait for its redeploy.
- **MT5 initialize failed:** open the Vantage desktop terminal, sign in, and check that it is connected.
- **XAUUSD not found:** use the exact symbol name shown in Market Watch.
- **No tick for GOLD:** check market session, Market Watch visibility, and whether Vantage is streaming quotes.
- **Vantage feed stale:** keep the bridge process and terminal running; confirm the backend URL and network access.

Never share the bridge secret in chat or commit it to the repository.


## Four-engine strategy architecture (Sections 15 onward)

The strategy extension is implemented as four separate runtime engines with one-way contracts:

1. **MBIE — Market Behaviour Intelligence:** analyses regime, trend/structure, liquidity candidates, candle-derived zones, volume behaviour, momentum, volatility, an OHLCV-based auction-profile proxy, scenarios and data coverage. It never emits an entry signal.
2. **Multi-Timeframe Engine:** combines completed Monthly, Weekly, Daily, 4H, 1H, 30M, 15M and 5M MBIE snapshots. It publishes directional agreement and conflicts; it does not rewrite the underlying timeframe analysis.
3. **Entry Engine:** consumes MBIE + MTF context and emits only a 1m MAIN or ADD-ON candidate after sweep/rejection, BOS/MSS, displacement and FVG/OB retest. It does not set quantity, SL or TP.
4. **Campaign Engine:** is the only engine allowed to build the order plan, estimate quantity, choose the directional liquidity target, set SL/TP, enforce per-position and total campaign risk, protect add-ons, and manage partial exits / invalidation.

### Data and risk configuration

- Optional estimated quantity requires `AST_CAMPAIGN_ACCOUNT_EQUITY` plus an asset-specific contract-value setting: `AST_CAMPAIGN_BTC_VALUE_PER_PRICE_UNIT` or `AST_CAMPAIGN_GOLD_VALUE_PER_PRICE_UNIT`. If either is absent, quantity is shown as unavailable rather than guessed.
- Add-ons are blocked until existing positions are protected at breakeven or better, a favorable price extension has occurred, and the total campaign risk limit remains satisfied.
- Historical bid/ask footprint, DOM, absorption and economic-news feeds are unavailable in the current candle replay and are explicitly marked unavailable.
- POC/VAH/VAL are currently a typical-price × candle-volume OHLCV proxy, not exchange trade-level volume profile. Session tags use UTC-window approximations until an exchange-calendar / DST-aware session feed is connected.
- Historical replay remains point-in-time and uses completed candles. Fees and slippage are excluded; all fills are simulated, and no broker orders are placed by this service.


The full strategy document is maintained in `AST_PYRAMID_STRATEGY_COMPLETE.md`: original Sections 1–14, MBIE Section 15, and the explicit four-engine responsibility architecture in Section 16.


## Auto-refresh and campaign Telegram notifications

- The dashboard refreshes automatically every 2 seconds by default. The header control can change this to 5 or 10 seconds or pause/resume browser refresh. The campaign API cache is also set to 2 seconds.
- Campaign notifications are checked by a server-side worker every 2 seconds while the Render service is running. It can alert for a confirmed 1m BUY/SELL setup awaiting next-open validation, a simulated position fill, a 50% partial target, and a simulated close (stop, final target or invalidation).
- Alerts use Telegram Bot API. Create a bot with Telegram's BotFather, open the bot chat and send `/start`, then set these environment variables in Render (Environment):
  - `ENABLE_TELEGRAM=true`
  - `TELEGRAM_BOT_TOKEN=<your bot token>`
  - `TELEGRAM_CHAT_ID=<your private chat id>`
- Save the environment changes and allow the service to redeploy. The dashboard header shows whether campaign Telegram alerts are configured; `/api/health` reports `campaign_alerts_enabled`.
- Keep the bot token private. Never add it to GitHub, frontend code or screenshots.
- Alerts explicitly describe analytical/simulated plans and fills. This application does not submit broker orders.
- Render Free web services can spin down after 15 minutes without inbound HTTP requests or messages on an existing inbound WebSocket connection. When the service is asleep, its background alert worker cannot monitor the market or send Telegram notifications. Reliable unattended alerts while the dashboard is closed require an always-on service plan. See Render's free-instance limitations.



## Balanced entry filters (default)

The live Entry Engine now uses a balanced threshold profile to improve setup frequency without removing the core sequence:
- Weighted MTF agreement: 60% minimum; 15m and 5m must still agree and Daily/4H/1H/15m/5m must all be ready.
- Entry score: 65/100; MBIE context score: 50/100; weighted input coverage: 40%.
- Sweep / rejection → BOS/MSS + displacement → same-direction FVG / OB retest with rejection are still required.
- A setup can remain valid for 24 completed 1m bars; BOS and displacement may be up to 6 bars apart.
- Choppy / ranging 1m conditions remain blocked. Campaign risk caps and minimum 1.5R directional liquidity target are unchanged.
- The dashboard reports the latest Entry Engine blocking reason. More signals are not a promise of higher win rate; evaluate them over a sufficiently large fee/slippage-adjusted sample.

