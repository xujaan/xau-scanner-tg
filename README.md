<div align="center">

# 🥇 XAU/USD (Gold) Precision Signal Bot

[![Python](https://img.shields.io/badge/Python-3.9+-blue?style=for-the-badge&logo=python&logoColor=white)](https://www.python.org/)
[![Telegram API](https://img.shields.io/badge/Telegram-API-0088CC?style=for-the-badge&logo=telegram&logoColor=white)](https://core.telegram.org/bots)
[![TwelveData](https://img.shields.io/badge/TwelveData-Market_Data-FF4B4B?style=for-the-badge)](https://twelvedata.com/)
[![Railway](https://img.shields.io/badge/Railway-Deploy-0B0D0E?style=for-the-badge&logo=railway&logoColor=white)](https://railway.app/)

*An institutional-grade algorithmic trading bot engineered specifically for Gold (XAU/USD). Utilizes Smart Money Concepts (SMC) including Market Structure, Liquidity Grabs, and Fair Value Gaps (FVG) to dispatch high-probability trade setups directly to Telegram.*

</div>

---

## 📑 Table of Contents
- [About the Project](#-about-the-project)
- [Algorithmic Strategy](#-algorithmic-strategy)
- [Key Features](#-key-features)
- [Requirements & Setup](#-requirements--setup)
- [Environment Variables](#-environment-variables)
- [Deployment (Railway/Heroku)](#-deployment)
- [Disclaimer](#-disclaimer)

---

## 🧠 About the Project

This bot monitors the **XAU/USD (Gold)** forex market autonomously 24/5. Rather than relying on lagging indicators, it implements core **Smart Money Concepts (SMC)** logic. It processes `1-Hour` candles to determine the macro bias and `5-Minute` candles to isolate surgical entry triggers. 

When a confluence of signals aligns with strict risk-management parameters, it formats a comprehensive report detailing the entry, exact stop loss, take profit, and the precise reasoning behind the trade—delivered instantly to your Telegram.

---

## ⚙️ Algorithmic Strategy

The bot scores potential trade setups out of 100 based on multiple confluences:

1. **Market Structure (BOS / CHoCH)**: Analyzes the last 50 candles to map swing highs and lows.
2. **Liquidity Grabs**: Identifies aggressive wick rejections beyond recent extremes.
3. **Fair Value Gaps (FVG)**: Detects impulsive momentum and structural imbalances.
4. **Key S&R Levels**: Calculates dynamic Support and Resistance zones.
5. **Price Action Patterns**: Engulfing candles, Hammers, and Shooting Stars.
6. **RSI Divergence**: Uses standard 14-period RSI to validate overbought/oversold extremes.

---

## ✨ Key Features

- **Dynamic Lot Sizing**: Uses a conservative risk profile (default **2%** of capital per trade). Lots are floored, not rounded up, and a setup whose minimum 0.01 lot would risk more than `2×` the target is rejected.
- **Capital Adequacy Checks**: Gold is volatile. The bot mathematically verifies if your account balance can handle the minimum lot size for a specific setup's stop-loss. If the risk is too high, it dynamically rejects the trade.
- **Portfolio Guards**: At most one open signal by default, a total heat cap (default 4% of capital), a daily loss brake (default 3R), and a blackout before the Friday close and the daily settlement break.
- **Journal Integrity**: Only closed candles are evaluated, each bar is processed once after it closes, and every alert stores its entry timestamp, actual risk, MFE and MAE. Records without an entry bar are marked unverified, excluded from hit-rate stats, and reported by `reconcile.py`.
- **Paper Trading Mode**: Safety-first testing. Generates "Paper Signals" for forward-testing without risking live capital.
- **Session Awareness**: Operates aware of Asian, London, and New York overlapping sessions.
- **No Calendar Cap**: Every qualifying setup is alerted — there is no calendar-day quota to run out of, only a 45-minute same-direction cooldown plus the portfolio risk guards and daily loss brake.
- **TwelveData by Default**: Candles come from TwelveData. An OANDA v20 practice account is supported but stays switched off until you explicitly enable it with `DATA_PROVIDER=oanda`.
- **Durable Archive (Turso/libSQL or SQLite)**: Closed candles are stored once per timeframe, every alert with its outcome, and every AI review — so hit rates can be rebuilt after a redeploy instead of resetting. Turso is used when `TURSO_DATABASE_URL` and `TURSO_AUTH_TOKEN` are set, otherwise a local SQLite file.
- **Signal Journal**: Every alert is recorded with its SL/TP and later resolved to TP or SL from the candles that follow, giving real hit rates per direction and confidence band instead of guesswork.
- **AI Second Opinion**: When `OPENROUTER_API_KEY` is set, a second Telegram message carries an LLM review of the rule signal (verdict, confidence, reasons) informed by those hit rates. The AI never changes entry/SL/TP and a failed AI call never blocks the alert.
- **Telegram Commands**: `/scan` for an immediate scan, plus `/price`, `/status`, `/stats`, `/journal`, `/settings`, `/pause`, `/resume` and `/help`. Only messages from `TELEGRAM_CHAT_ID` are accepted, and outside market hours the bot replies that the market is closed.

### Telegram commands

| Command | What it does | API cost |
|---|---|---|
| `/scan` | Run a scan immediately instead of waiting for the next candle close | 2 requests |
| `/price` | Current price, 15M bias and 5M RSI | 2 requests |
| `/status` | Market open/closed, data provider, DB backend, AI state, alerts paused | – |
| `/stats` | Hit rate overall, per direction, per confidence band, and per AI verdict | – |
| `/journal` | Last 5 alerts with TP / SL / OPEN outcome | – |
| `/settings` | Active configuration (provider, cap, cooldown, model) | – |
| `/pause`, `/resume` | Stop and resume alert sending; scanning continues | – |
| `/help` | List the commands | – |
- **Market-Hour Aware**: Skips the weekend and the daily 17:00–18:00 ET settlement break, evaluated in New York time so DST changes are handled.

---

## 🛠️ Requirements & Setup

### Prerequisites
- Python 3.9+
- A Telegram Bot Token (from [@BotFather](https://t.me/botfather))
- A TwelveData API Key (for real-time XAU/USD data)

### Local Installation

1. **Clone the repository:**
   ```bash
   git clone https://github.com/YourUsername/GJsignal-bot.git
   cd GJsignal-bot
   ```

2. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

3. **Configure the script:**
   Inside `bot.py`, locate the Configuration block and update your risk settings:
   ```python
   CAPITAL = 200               # Your exact real balance
   RISK_PERCENT = 0.02         # 2% risk per trade
   MIN_CONFIDENCE = 65         # Strict 65%+ confluence required
   PAPER_MODE = True           # Set False ONLY when ready for live execution
   ```

   There is **no cap on how many signals the bot sends**. Every setup that passes the confluence score is alerted; the only throttle is a 45-minute same-direction cooldown so a repeat setup is not re-sent. Override these with environment variables (defaults shown):
   ```python
   MAX_TRADES_PER_DAY = 0                       # 0 = unlimited; a positive number re-enables the rolling cap
   SIGNAL_WINDOW_HOURS = 24                     # rolling window used only when a cap is set
   STATE_FILE = "signal_state.json"             # persisted history, survives restarts
   ```

4. **Run the bot:**
   ```bash
   python bot.py
   ```

5. **Audit old journal outcomes (optional but recommended after an upgrade):**
   ```bash
   python reconcile.py            # dry run, prints every outcome that would change
   python reconcile.py --apply    # write the rebuilt outcomes
   ```
   This rebuilds first-touch TP/SL from the stored closed bars. Records without an `entry_bar` are reported as unverifiable and are left untouched.

---

## 🔑 Environment Variables

To keep your credentials secure, do **not** hardcode them. Export these environment variables locally or input them into your hosting provider's dashboard:

| Variable | Description |
|---|---|
| `TELEGRAM_TOKEN` | The HTTP API token from BotFather. |
| `TELEGRAM_CHAT_ID` | Your personal ID or Channel ID where signals will be sent. |
| `TWELVEDATA_API_KEY` | Free API key from TwelveData to pull candlestick data. Used as a fallback. |
| `DATA_PROVIDER` | Optional. `twelvedata` (default) or `oanda`. OANDA is never called unless this is set to `oanda`. |
| `OANDA_API_KEY` | Optional. Personal access token from a **practice** account at OANDA. Only used with `DATA_PROVIDER=oanda`. |
| `OANDA_ACCOUNT_ID` | Optional. The v20 account ID that goes with `OANDA_API_KEY`. Only used with `DATA_PROVIDER=oanda`. |
| `OANDA_ENV` | Optional. `practice` (default) or `live`; selects the OANDA host. |
| `OANDA_INSTRUMENT` | Optional. Instrument to pull, default `XAU_USD`. |
| `MAX_TRADES_PER_DAY` | Optional. Max signals in any rolling window; `0` (default) = unlimited. |
| `SIGNAL_WINDOW_HOURS` | Optional. Rolling window length in hours, used only when a cap is set (default `24`). |
| `STATE_FILE` | Optional. File used to persist signal history (default `signal_state.json`). |
| `CAPITAL` | Optional. Account balance used for sizing (default `200`). |
| `RISK_PERCENT` | Optional. Target risk per signal as a fraction (default `0.02` = 2%). |
| `MAX_RISK_MULTIPLE` | Optional. Reject a signal when the minimum lot would risk more than this multiple of the target (default `2.0`). |
| `MAX_OPEN_POSITIONS` | Optional. Maximum concurrent open alerts (default `1`). |
| `MAX_PORTFOLIO_HEAT_PCT` | Optional. Total open risk as a fraction of capital (default `0.04` = 4%). |
| `DAILY_LOSS_LIMIT_R` | Optional. Stop sending new alerts after this many closed R today (default `3.0`). |
| `WEEKEND_BLACKOUT_HOURS` | Optional. No new entries this many hours before the Friday close (default `2`). |
| `DAILY_BREAK_BLACKOUT_MINUTES` | Optional. No new entries this many minutes before the daily settlement break (default `30`). |
| `OPENROUTER_API_KEY` | Optional. Enables the AI second opinion. Without it the AI layer stays off. |
| `OPENROUTER_MODEL` | Optional. Default `deepseek/deepseek-v4.1-flash-1017`. Pin a dated snapshot ID. |
| `AI_TIMEOUT` | Optional. Seconds before the AI call is abandoned (default `20`). |
| `JOURNAL_FILE` | Optional. Where sent signals and their outcomes are stored (default `signal_journal.json`). |
| `TURSO_DATABASE_URL` | Optional. Turso/libSQL URL, e.g. `libsql://mydb.turso.io`. Used together with `TURSO_AUTH_TOKEN`. |
| `TURSO_AUTH_TOKEN` | Optional. Token for the Turso database. Without both Turso values the bot uses local SQLite. |
| `DB_FILE` | Optional. SQLite file used when Turso is not configured (default `signals.db`). |

---

## 🚀 Deployment

The project is fully pre-configured for seamless cloud deployment to ensure 24/5 uptime.

### Railway.app (Recommended)
This repository includes a `railway.json` and `Procfile`. 
1. Link your GitHub repository to a new Railway project.
2. Add your Environment Variables in the Railway Dashboard.
3. The bot will deploy and run automatically.

---

## ⚠️ Disclaimer

**Educational Purposes Only.** Trading Gold (XAU/USD) involves significant risk of loss and is not suitable for all investors. The algorithms provided in this repository do not constitute financial advice. Always use `PAPER_MODE` before engaging in live markets. Past performance does not guarantee future results.

---
<div align="center">
  <i>May your Stop Losses be tight and your Take Profits be hit.</i>
</div>
