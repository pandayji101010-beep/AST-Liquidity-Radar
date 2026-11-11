# AST Institutional Liquidity Radar — BTC + GOLD

Yeh first runnable release hai. Binance USDⓈ-M public market data se BTCUSDT aur XAUUSDT ko read karta hai. Vantage ka apna executable quote feed local Vantage MetaTrader 5 terminal se liya jaata hai, phir Binance prices/levels ko Vantage quote basis mein map kiya jaata hai.

## Is version mein kya hai

- Live Binance mark price, funding rate aur public order-book depth (top 20 levels).
- Aggregate trades se approximate CVD (buyer/seller taker direction).
- Open Interest aur global long/short account ratio polling.
- Binance force-order liquidation snapshots ka rolling 5-minute aggregation aur recent price clusters.
- Binance 1m / 5m / 15m / 1h candles par EMA trend, recent range, equal highs/lows, wick sweeps aur displacement-break detection.
- Rule-based Bullish/Bearish Pressure Index. Yeh probability ya guaranteed signal nahi hai.
- Local Vantage MT5 tick bridge + rolling median price ratio calibration.
- Web dashboard aur optional Telegram alerts.

## Uploaded Abhay Patil Black Box Trap Strategy

Dashboard ka trap scanner uploaded Pine v5 file ke rules ko har available timeframe (1m, 5m, 15m, 30m, 1h, 4h, 1d) par historical closed candles ke oldest-to-newest replay se evaluate karta hai. Yeh generic wick-sweep + BOS strategy nahi hai.

- Swing High / Low Lookback: 20 left bars + 20 right bars; pivot tabhi active hota hai jab right-side ke 20 candles close ho chuke hon.
- Bear Trap / BUY: candle ka close active support ke neeche cross kare, phir later green candle support ke upar close kare. Us reclaim candle ka high BUY STOP entry aur low SL hota hai.
- Bull Trap / SELL: candle ka close active resistance ke upar cross kare, phir later red candle resistance ke neeche close kare. Us reclaim candle ka low SELL STOP entry aur high SL hota hai.
- Source strategy mein pending stop order ka expiry nahi hai; woh trigger hone ya newer setup se replace hone tak pending rehta hai.
- Exit: reclaim candle ke range ke basis par ek hi TP, default 1:2 risk/reward. Uploaded source mein alag TP1/TP2 nahi hain.
- Historical statistics sirf available OHLC history par bar-based simulation hain. Agar SL aur TP ek hi candle mein touch hon, scanner conservative result ke liye SL pehle count karta hai. Actual spread, slippage aur TradingView broker-emulator fills alag ho sakte hain.

### Historical data depth

- Backend maximum 25,000 closed candles per timeframe retain/replay karta hai. Actual count Vantage history availability, Binance archive availability aur MT5 terminal setting par depend karta hai.
- Vantage MT5 mein Tools → Options → Charts → Max bars in chart ko kam-se-kam 25,000 set karein.
- Latest mt5_bridge.py download karke bridge restart karein, taaki 25,000-bar history bootstrap ho. Dashboard par har card mein scanned candles aur first/last data time dikhte hain.
- BTC ke 1m, 5m, 15m, 30m, 1h, 4h aur 1d candles alag-alag native Binance USD-M archives se load hote hain; har timeframe par 25,000 closed candles tak replay hota hai. Native interval archive unavailable ho tabhi 1m se resampling fallback hoti hai. GOLD XAUUSD history directly Vantage MT5 se aati hai. Dono assets ke price sources mix nahi hote.

## Data source aur limitations

- BTC: Binance USDⓈ-M BTCUSDT perpetual.
- Gold: Binance USDⓈ-M XAUUSDT TradFi perpetual. Binance ne XAUUSDT ko 5 Jan 2026 ko launch kiya. Commodity underlying session ke bahar Binance ka price-index calculation mode alag ho sakta hai; market session state backend mein capture hota hai.
- Vantage price: aapke apne logged-in Vantage MT5 terminal se bid/ask tick. `mt5_bridge.py` MetaTrader5 Python integration ko local MT5 terminal ke through use karta hai. Login details ya broker password script mein hardcode nahi hote.
- Price mapping rolling median ratio se hoti hai: `Vantage MT5 bid / nearest Binance mark price`. Kam se kam 3 timestamp-matched tick samples chahiye. Stale feed par adjusted levels hide ho jaate hain.
- Agar aap TradingView chart dekhte hain, Vantage MT5 quote aur TradingView chart display mein chhota feed/bid-ask difference ho sakta hai. Dashboard ke adjusted levels ko chart par compare karke validate karein.
- Binance order book sirf us exchange ki visible liquidity hai; hidden orders / dusre venues ismein nahi aate. RPI orders depth se excluded ho sakte hain.
- Liquidation stream throttled snapshot feed hai; yeh har liquidation ka complete audit log nahi.
- Open Interest, global long/short ratio aur funding exchange-level aggregates hain; actual individual retail positions nahi dikhate.
- Backend read-only hai. Koi Binance/Vantage order place ya modify nahi karta.

## Local setup (Windows PC)

Recommended: Windows 10/11, 64-bit Python 3.12, Vantage MT5 terminal installed and logged in to your account.

1. Project folder extract karein.
2. `.env.example` ko `.env` naam se copy karein.
3. `.env` mein `VANTAGE_BRIDGE_SECRET` ko 24+ characters ke unique random secret se replace karein. Secret banane ke liye `python -c "import secrets; print(secrets.token_urlsafe(32))"` chala sakte hain. Default `REPLACE_...` value par bridge start nahi hoga. Same value backend aur MT5 bridge ke liye rahegi.
4. `run_windows.bat` double-click karein. Backend/dashboard sirf local PC (`127.0.0.1`) par start hoga.
5. Browser mein `http://127.0.0.1:8000` open karein. Is window ko chalne dein.
6. MT5 terminal open rakhein aur Vantage account mein login rahein.
7. `.env` mein `MT5_BTC_SYMBOL` aur `MT5_GOLD_SYMBOL` ko Market Watch ke exact symbol names se match karein. Agar suffix laga ho (e.g. `.a`, `-pro`) to exact name likhein.
8. `run_mt5_bridge_windows.bat` ko doosri terminal window mein run karein.
9. Dashboard mein Vantage bridge ke `CALIBRATED` hone ka wait karein; kam se kam 3 matched tick samples chahiye.

Local `.env` example:

```env
VANTAGE_BRIDGE_SECRET=put-a-long-random-string-here
AST_BACKEND_URL=http://127.0.0.1:8000
MT5_BTC_SYMBOL=BTCUSD
MT5_GOLD_SYMBOL=XAUUSD
MT5_PRICE_SIDE=bid
MT5_POLL_SECONDS=0.5
MT5_PATH=
ENABLE_TELEGRAM=false
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
```

**Important:** MT5 bridge ko usi Windows PC par chalayein jahan Vantage MT5 terminal installed aur logged in hai. Agar backend VPS par host hai, `AST_BACKEND_URL` ko apne HTTPS server URL se replace karein. Public internet par backend expose karne se pehle strong secret aur HTTPS configure karein.

## Symbols kaise check karein

MT5 → View → Market Watch → right-click → Symbols. BTC aur Gold ke exact broker symbol names copy karke `.env` mein set karein. Agar default `BTCUSD` / `XAUUSD` nahi milta, bridge matching symbols ki list error message mein print karega.

## Telegram setup (optional)

1. Telegram par `@BotFather` se bot banayein aur token lein.
2. Bot ko ek message bhejein, phir Bot API `getUpdates` response se chat ID nikaalein.
3. `.env`:

```env
ENABLE_TELEGRAM=true
TELEGRAM_BOT_TOKEN=your_bot_token
TELEGRAM_CHAT_ID=your_chat_id
```

4. Backend restart karein. Same event type per asset 5-minute cooldown ke saath alerts aayenge.

## Remote VPS setup (optional)

- Backend VPS/host par run karein, `VANTAGE_BRIDGE_SECRET` set karein, valid HTTPS enable karein.
- Vantage MT5 terminal aur `mt5_bridge.py` apne Windows PC par run karein.
- Local bridge `.env` mein `AST_BACKEND_URL=https://YOUR-HOST` set karein.
- Bridge secret server ke secret se exactly match hona chahiye.

## Endpoints

- `/` — dashboard
- `/api/health` — health
- `/api/snapshot?timeframe=5m` — live data + analysis JSON
- `/api/events` — recent confirmed events
- `/api/vantage/mt5` — authenticated MT5 tick receiver
- `/docs` — FastAPI API docs

## Official documentation

- Binance USDⓈ-M Futures API / WebSocket: https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api
- Binance market streams: https://developers.binance.com/docs/derivatives/usds-margined-futures/websocket-market-streams
- MetaTrader 5 Python integration: https://www.mql5.com/en/docs/python_metatrader5
- Vantage MT5 platform guide: https://www.vantagemarkets.com/trading-platforms/metatrader-5/

## Interpretation guide

- **BSL**: buy-side liquidity estimate above highs; possible buy stops/short stops.
- **SSL**: sell-side liquidity estimate below lows; possible sell stops/long stops.
- **Upper sweep**: high takes prior range high but closes back below; potential breakout-buyer trap.
- **Lower sweep**: low takes prior range low but closes back above; potential breakout-seller trap.
- **CVD**: signed taker volume estimate from Binance aggregate trades.
- **Order-book imbalance**: visible top-20 bid vs ask notional. It changes quickly and includes cancellable orders.
- **Liquidation cluster**: recent reported liquidation prints grouped by approximate price buckets; not a forward liquidation heatmap.
- **Pressure index**: transparent heuristic score (0–100), not a calibrated probability or an estimate of a specific operator's intent.

## Before using live

- Run in observation mode first. Confirm Binance streams are live, OI/L-S are populated, Vantage basis ratio has at least 3 matched samples, and feed ages are fresh.
- Compare mapped levels against the Vantage chart during active BTC and Gold sessions.
- Check XAUUSDT session availability; its reference index can behave differently outside the underlying commodity's regular session.
- Backtest and forward-test event definitions. No claim is made about win rate or expected profit.
