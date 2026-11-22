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

> 📚 **Divergence deep-dive:** see [`docs/DIVERGENCE.md`](docs/DIVERGENCE.md)
> for the full pattern taxonomy, detection rules, backtest tables, live config,
> cent-account sizing, and caveats.

## ⚙️ Algorithmic Strategy

The bot scores potential trade setups out of 100 based on multiple confluences:

1. **Market Structure (BOS / CHoCH)**: Analyzes the last 50 candles to map swing highs and lows.
2. **Liquidity Grabs**: Identifies aggressive wick rejections beyond recent extremes.
3. **Fair Value Gaps (FVG)**: Detects impulsive momentum and structural imbalances.
4. **Key S&R Levels**: Calculates dynamic Support and Resistance zones.
5. **Price Action Patterns**: Engulfing candles, Hammers, and Shooting Stars.
6. **Divergence**: Regular and hidden divergence on RSI(14), Stochastic (9,3,5 and 14,3,3) and MACD histogram. Pivots are only read after their right-side bars close, so the live bot and the backtest see the same confirmed swing.
7. **Stochastic**: Slow stochastic with configurable `STOCH_K/STOCH_D/STOCH_SMOOTH` (defaults 9/3/5), used as a divergence source with a 25/75 zone filter. Divergence is computed on every scan; the raw signal is recorded in shadow, while live alerts pass the OB-edge, HTF-EMA, hidden-divergence and ADX gates by default. Set the `REQUIRE_*` gates to `0` and `MIN_ADX=0` to return to the old baseline.

---

## ✨ Key Features

- **Dynamic Lot Sizing**: Uses a conservative risk profile (default **0.5%** of capital per trade; the old default was 2%). Lots are floored, not rounded up, and a setup whose minimum 0.01 lot would risk more than `2×` the target is rejected. A $200 account cannot size the divergence combo's ~$10 stop within that cap; set `CAPITAL` to the real balance.
- **Capital Adequacy Checks**: Gold is volatile. The bot mathematically verifies if your account balance can handle the minimum lot size for a specific setup's stop-loss. If the risk is too high, it dynamically rejects the trade.
- **Portfolio Guards**: At most one open signal by default, a total heat cap (default 4% of capital), a daily loss brake (default 3R), and a blackout before the Friday close and the daily settlement break.
- **Journal Integrity**: Only closed candles are evaluated, each bar is processed once after it closes, and every alert stores its entry timestamp, actual risk, MFE and MAE. Records without an entry bar are marked unverified, excluded from hit-rate stats, and reported by `reconcile.py`.
- **Shadow Variants**: Every scan is also recorded under several filters — baseline, volatility, LONG-only, SHORT-only, SHORT + minimum ATR, no-Asia (04:00–06:59 UTC), confidence 70–79, 15M EMA trend, 15M EMA + SHORT, divergence variants (any/regular/hidden, Stochastic 9,3,5, Stochastic 14,3,3, RSI, MACD, OBV), 15M divergence confirmation, ADX strength, Stochastic cross, OB-edge, OB-edge + 15M EMA, and OB-edge + 15M EMA + hidden divergence — resolved by the same closed-bar evaluator, with `/shadow` showing cost-adjusted R. Shadows never send alerts. They evaluate the signal selected by the live engine; they do not search for a second setup when that direction is filtered out.
- **Volatility Regime Gate**: Live alerts are suppressed when the 5M ATR is below `MIN_ENTRY_ATR` (default `4.0`). The 70-day replay cut drawdown from 26.2R to 13.4R and raised E[R] from +0.124 to +0.163 by removing low-volatility chop.
- **Bounded Self-Learning**: `/learn` ranks the shadow variants against the live champion using net E[R] and drawdown, with a minimum sample before promotion. `/learnai` sends the aggregate live/shadow/regime summary to the AI for a post-mortem and one bounded recommendation. The AI cannot invent parameters or auto-apply changes; promotion remains a deliberate config change. Auto-promotion is opt-in, waits for 100 closed shadow trades, requires a two-direction sample for direction filters, and has a 14-day cooldown.
- **Paper Trading Mode**: Safety-first testing. Generates "Paper Signals" for forward-testing without risking live capital.
- **Session Awareness**: Operates aware of Asian, London, and New York overlapping sessions.
- **No Calendar Cap**: Every qualifying setup is alerted — there is no calendar-day quota to run out of, only a 45-minute same-direction cooldown plus the portfolio risk guards and daily loss brake.
- **MT5 Primary, TwelveData Fallback**: When `MT5_API_KEY` is set, candles come from the broker's MT5 REST bridge — the same feed as execution, with per-bar spread and volume. The bridge stamps bars in broker server time (Headway is GMT+2/GMT+3), so `mt5_data.py` converts every timestamp to UTC before the engine sees it. TwelveData remains the fallback; OANDA stays opt-in with `DATA_PROVIDER=oanda`.
- **Durable Archive (Turso/libSQL or SQLite)**: Closed candles are stored once per timeframe, every alert with its outcome, and every AI review — so hit rates can be rebuilt after a redeploy instead of resetting. Turso is used when `TURSO_DATABASE_URL` and `TURSO_AUTH_TOKEN` are set, otherwise a local SQLite file.
- **Signal Journal**: Every alert is recorded with its SL/TP and later resolved to TP or SL from the candles that follow, giving real hit rates per direction and confidence band instead of guesswork.
- **AI Second Opinion**: When `OPENROUTER_API_KEY` is set, a second Telegram message carries an LLM review of the rule signal (verdict, confidence, reasons) informed by those hit rates. The AI never changes entry/SL/TP and a failed AI call never blocks the alert. Empty or null model content is retried once before the review is skipped.
- **Telegram Commands**: `/scan` for an immediate scan, plus `/price`, `/status`, `/stats`, `/shadow`, `/checkpoint`, `/journal`, `/settings`, `/pause`, `/resume` and `/help`. Only messages from `TELEGRAM_CHAT_ID` are accepted, and outside market hours the bot replies that the market is closed.

### Telegram commands

| Command | What it does | API cost |
|---|---|---|
| `/scan` | Run a scan immediately instead of waiting for the next candle close | 2 requests |
| `/price` | Current price, 15M bias and 5M RSI | 2 requests |
| `/status` | Market open/closed, data provider, DB backend, AI state, alerts paused | – |
| `/stats` | Hit rate overall, per direction, per confidence band, and per AI verdict | – |
| `/shadow` | Compare shadow variants (baseline, long/short-only, volatility, no-Asia) with cost-adjusted R | – |
| `/checkpoint` | Progress toward the 100-trade review: live WR/E[R]/DD and shadow closed counts | – |
| `/learn` | Rank shadow variants, show the champion and the statistical promotion rule | – |
| `/learnai` | Ask the AI for a bounded post-mortem/recommendation | 1 AI call |
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
   RISK_PERCENT = 0.005        # 0.5% risk per trade
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

### Research / backtest

The live engine is replayed over cached bars. With `MT5_API_KEY` set, the
default feed is the broker's XAUUSD history; otherwise it falls back to Yahoo
GC=F.

```bash
# MT5 spot, one year, using each entry bar's broker spread
python backtest.py --data mt5 --start 2025-11-21 --end 2026-11-21 \
  --bar-spread --refresh

# only divergence variants
python backtest.py --data mt5 --bar-spread --only "div"

# live-like portfolio gates: max 1 open, daily 3R brake, min-lot risk cap
python backtest.py --data mt5 --bar-spread --portfolio \
  --capital 10000 --risk-percent 0.005 \
  --only "OB+EMA+div+ATR4+ADX20"

# Yahoo GC=F fallback
python backtest.py --spread 0.20
```

P2 entry-timing and divergence experiments live only in `backtest.py`; they are
not live defaults. Use two date ranges (`--start` / `--end`) before treating any
variant as stable. `--bar-spread` is deliberately stricter than the old flat
`--spread 0.20`: the broker's XAUUSD spread is commonly $0.30–$0.45 and widens
around the close.

Standalone divergence filters lost money in the 1-year replay. The combination
that stayed positive in both halves was **OB edge + 15M EMA + hidden
divergence + ATR4** (+37.71R then +46.04R; E[R] 0.132; 18.21R max drawdown over
675 trades). Adding **ADX >= 20** improved it to +29.27R/+42.36R, E[R] 0.163 and
12.26R drawdown. Hidden divergence is useful as a filter, not as a standalone
entry. It is available as the `ob_edge_htf_div` shadow variant and can be
replicated with:

```env
REQUIRE_OB_EDGE=1
REQUIRE_HTF_EMA=1
REQUIRE_DIVERGENCE=1
DIVERGENCE_FILTER_TYPES=hidden
ATR_STOP_MULT=1.5
MIN_ENTRY_ATR=4.0
MIN_ADX=20
```

`DIVERGENCE_LIVE_MODE=shadow` is the default and keeps the raw signal visible
to the shadow logger; the live gate is `REQUIRE_DIVERGENCE`. The optional
`DIVERGENCE_LIVE_MODE=filter` pre-filters inside the engine and hides the raw
baseline from `/shadow`, so it is not the recommended setting.

`MIN_ADX=20` was the most consistent Round 2 addition in split-half testing
(12.26R max drawdown); `MIN_ADX=25` is more selective and has a higher
full-year E[R] but a smaller, second-half-heavy sample. With live-like portfolio
gates (max one open position, 0.5% risk, daily 3R), ADX20 made +50.53R,
E[R] +0.126, DD 16.02 over 400 trades. Keep risk at **0.5–1%** per trade while
it is unproven. Set `CAPITAL` to the real account balance: with the default
`CAPITAL=200`, the combo's ~$10 average stop is above the min-lot risk cap and
the bot will reject many signals. `BACKTEST.md` has the full table and caveats.

---

### Live demo execution on a cent account

The Headway demo account is a **USC cent account**: a displayed balance of
`10,000.00` means **$100.00**. Keep the bot's capital in the same account-currency
units:

```env
PAPER_MODE=0
EXECUTION_ENABLED=1
CENT_ACCOUNT=1
CAPITAL=10000
RISK_PERCENT=0.005
MT5_API_KEY=...
MT5_SYMBOL=XAUUSD
```

With this, `RISK_AMOUNT` is 50 USC = **$0.50** per trade, and the bot sizes the
0.01-lot cent contract correctly. Do **not** set `CAPITAL=100` for this account;
that would make the min-lot risk gate reject nearly every signal. The live gate
still requires MT5 bridge connectivity and `trade_allowed` on both the terminal
and the account.

## 🔑 Environment Variables

To keep your credentials secure, do **not** hardcode them. Export these environment variables locally or input them into your hosting provider's dashboard:

| Variable | Description |
|---|---|
| `TELEGRAM_TOKEN` | The HTTP API token from BotFather. |
| `TELEGRAM_CHAT_ID` | Your personal ID or Channel ID where signals will be sent. |
| `TWELVEDATA_API_KEY` | Optional fallback feed. Used after MT5 when configured. |
| `MT5_API_URL` | Optional. MT5 bridge base URL (default `https://mt5-api.gnt.my.id`). |
| `MT5_API_KEY` | Optional. `X-API-Key` for the MT5 bridge. When set, MT5 becomes the primary feed. |
| `MT5_SYMBOL` | Optional. Bridge symbol, default `XAUUSD` (not the chart suffix `XAUUSD.m`). |
| `MT5_SERVER_TZ` | Optional. Broker server timezone for UTC normalisation, default `Europe/Kyiv` (GMT+2/GMT+3). |
| `DATA_PROVIDER` | Optional. `mt5` (default when `MT5_API_KEY` is set), `auto`, `twelvedata`, or `oanda`. |
| `STOCH_K` / `STOCH_D` / `STOCH_SMOOTH` | Optional. Slow-stochastic settings, defaults `9/3/5`. |
| `DIVERGENCE_ENABLED` | Optional. Compute divergences on every scan (default `1`). |
| `DIVERGENCE_LIVE_MODE` | Optional. `shadow` (default: raw signal, live gate in `risk_gate`), `bonus`, `filter` (pre-filter; hides the raw baseline from shadow), or `off`. |
| `REQUIRE_DIVERGENCE` | Optional. Live gate requiring a divergence that matches the signal, default `1`; types come from `DIVERGENCE_FILTER_TYPES`. |
| `DIVERGENCE_FILTER_TYPES` | Optional. Comma-separated types for the live gate, default `hidden`. |
| `DIVERGENCE_BONUS` | Optional. Score bonus used when mode is `bonus`, default `10`. |
| `ATR_STOP_MULT` | Optional. Stop distance in ATR(14) units, default `1.5`. The 1-year MT5 replay improved with `2.0` for the raw engine, but `1.5` was better once the OB-edge + 15M-EMA filters were applied. |
| `REQUIRE_OB_EDGE` | Optional. Require entry in the protective half of the order block, default `1`. |
| `REQUIRE_HTF_EMA` | Optional. Require 15M EMA(20/50) trend alignment, default `1`. |
| `MIN_ADX` / `MAX_ADX` | Optional. Require 5M ADX(14) above/below a threshold. `MIN_ADX=20` is the default; `MAX_ADX=0` is off. |
| `OANDA_API_KEY` | Optional. Personal access token from a **practice** account at OANDA. Only used with `DATA_PROVIDER=oanda`. |
| `OANDA_ACCOUNT_ID` | Optional. The v20 account ID that goes with `OANDA_API_KEY`. Only used with `DATA_PROVIDER=oanda`. |
| `OANDA_ENV` | Optional. `practice` (default) or `live`; selects the OANDA host. |
| `OANDA_INSTRUMENT` | Optional. Instrument to pull, default `XAU_USD`. |
| `MAX_TRADES_PER_DAY` | Optional. Max signals in any rolling window; `0` (default) = unlimited. |
| `SIGNAL_WINDOW_HOURS` | Optional. Rolling window length in hours, used only when a cap is set (default `24`). |
| `STATE_FILE` | Optional. File used to persist signal history (default `signal_state.json`). |
| `CAPITAL` | Optional. Account balance used for sizing (default `200`). |
| `RISK_PERCENT` | Optional. Target risk per signal as a fraction (default `0.005` = 0.5%; 0.5–1% is the sane range for this strategy). |
| `PAPER_MODE` | Optional. `1` (default) sends signals without orders; set `0` for live/demo execution. |
| `EXECUTION_ENABLED` | Optional. `1` places the signal through the MT5 bridge; default `0`. Requires `PAPER_MODE=0`. |
| `CENT_ACCOUNT` | Optional. `1` for a USC cent account; divides capital/risk display by 100 while keeping the sizing math in account currency. |
| `MT5_MAGIC` / `MT5_FILLING` | Optional. Order magic (default `20261122`) and filling mode (default `FOK`). |
| `MAX_RISK_MULTIPLE` | Optional. Reject a signal when the minimum lot would risk more than this multiple of the target (default `2.0`). |
| `MAX_OPEN_POSITIONS` | Optional. Maximum concurrent open alerts (default `1`). |
| `MAX_PORTFOLIO_HEAT_PCT` | Optional. Total open risk as a fraction of capital (default `0.04` = 4%). |
| `DAILY_LOSS_LIMIT_R` | Optional. Stop sending new alerts after this many closed R today (default `3.0`). |
| `WEEKEND_BLACKOUT_HOURS` | Optional. No new entries this many hours before the Friday close (default `2`). |
| `DAILY_BREAK_BLACKOUT_MINUTES` | Optional. No new entries this many minutes before the daily settlement break (default `30`). |
| `MIN_ENTRY_ATR` | Optional. Live volatility floor: suppress alerts when 5M ATR is below this value (default `4.0`; set `0` to disable). |
| `SHADOW_ENABLED` | Optional. Record shadow variants on every scan (default `1`; set `0` to disable). |
| `SHADOW_MIN_ATR` | Optional. Minimum 5M ATR for the `short_vol` shadow variant (default `4.0`). |
| `SHADOW_SPREAD` | Optional. Round-trip cost in $/oz used by `/shadow` (default `0.20`). |
| `LEARNING_ENABLED` | Optional. Enable champion/challenger ranking and AI review (default `1`). |
| `LEARNING_MIN_TRADES` | Optional. Minimum closed shadow trades before a variant can be promoted (default `50`). |
| `LEARNING_AI` | Optional. Let the bot request a bounded AI post-mortem (default `1`). |
| `LEARNING_AI_HOURS` | Optional. Minimum hours between automatic AI reviews (default `24`). |
| `LEARNING_AUTO_PROMOTE` | Optional. Allow a qualified challenger to replace the live policy automatically (default `0`). |
| `LEARNING_LIVE_MIN_TRADES` | Optional. Closed shadow trades required before auto-promotion (default `100`). |
| `POLICY_PROMOTION_COOLDOWN_HOURS` | Optional. Minimum hours between automatic policy changes (default `336` = 14 days). |
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
