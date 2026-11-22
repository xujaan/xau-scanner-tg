"""Replay the live signal engine over historical candles.

Every signal here is produced by bot.generate_signal(), the same function the bot
runs every 5 minutes — this harness only supplies history and simulates fills.
That keeps the test honest: if the strategy changes, the backtest changes with it.

Data: Yahoo Finance chart API (GC=F, 5m bars, up to 60 days), cached as CSV.
GC=F is gold futures, not XAU/USD spot; the level differs by the basis, but the
R-multiple outcomes the harness measures are still representative.

Usage:
    python backtest.py                 # 60d, baseline + variants
    python backtest.py --refresh       # re-download the CSV
    python backtest.py --min-conf 70   # move the signal threshold
"""

import argparse
import bisect
import calendar
import contextlib
import csv
import io
import json
import os
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import bot
import mt5_data

DATA_DIR = Path(__file__).parent / "backtest_data"
YAHOO = ("https://query1.finance.yahoo.com/v8/finance/chart/"
         "{symbol}?interval=5m&range={rng}")

# generate_signal() is independent of the post-signal variant filters. Computing
# it once per bar and reusing the result across variants turns a 40-variant run
# from 40 full engine passes into one pass plus cheap filtering.
_SIGNAL_CACHE = {}

# ============================================
# DATA
# ============================================
def fetch_yahoo(symbol="GC=F", rng="60d"):
    req = urllib.request.Request(YAHOO.format(symbol=symbol, rng=rng),
                                 headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=40) as fh:
        payload = json.load(fh)
    res = payload["chart"]["result"][0]
    quote = res["indicators"]["quote"][0]
    bars, seen = [], set()
    for t, o, h, l, c in zip(res["timestamp"], quote["open"], quote["high"],
                             quote["low"], quote["close"]):
        if None in (o, h, l, c) or t in seen:
            continue
        seen.add(t)
        bars.append({"time": int(t), "open": float(o), "high": float(h),
                     "low": float(l), "close": float(c)})
    bars.sort(key=lambda b: b["time"])
    return bars


def load_bars(refresh=False):
    DATA_DIR.mkdir(exist_ok=True)
    path = DATA_DIR / "gc_5m.csv"
    if refresh or not path.exists():
        bars = fetch_yahoo()
        with open(path, "w", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(["time", "open", "high", "low", "close"])
            for b in bars:
                writer.writerow([b["time"], b["open"], b["high"], b["low"], b["close"]])
        print(f"cached {len(bars)} bars -> {path}")
        return bars
    bars = []
    with open(path, newline="") as fh:
        for row in csv.DictReader(fh):
            bars.append({"time": int(row["time"]), "open": float(row["open"]),
                         "high": float(row["high"]), "low": float(row["low"]),
                         "close": float(row["close"])})
    return bars


def load_bars_mt5(start=None, end=None, refresh=False, path=None):
    """Fetch XAUUSD 5m bars from the MT5 bridge and cache them as UTC epochs.

    The bridge returns broker server time; mt5_data converts every bar to UTC
    before it reaches this function. The CSV therefore matches the Yahoo path
    and the live bot's bar_epoch() convention.
    """
    DATA_DIR.mkdir(exist_ok=True)
    path = Path(path) if path else DATA_DIR / "xauusd_mt5_5m.csv"
    if refresh or not path.exists():
        client = mt5_data.MT5Client()
        end_dt = end or datetime.now(timezone.utc)
        start_dt = start or (end_dt - timedelta(days=180))
        bars = client.get_history("5min", start_dt, end_dt)
        with open(path, "w", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(["time", "open", "high", "low", "close",
                             "tick_volume", "spread"])
            for bar in bars:
                writer.writerow([
                    int(bot.bar_epoch(bar["time"])),
                    bar["open"], bar["high"], bar["low"], bar["close"],
                    bar.get("tick_volume", 0), bar.get("spread", 0),
                ])
        print(f"cached {len(bars)} MT5 bars -> {path}")
        # Fall through and read the CSV back so the caller always sees epoch
        # integers, whether the data came from the network or the cache.

    bars = []
    with open(path, newline="") as fh:
        for row in csv.DictReader(fh):
            bars.append({
                "time": int(row["time"]),
                "open": float(row["open"]), "high": float(row["high"]),
                "low": float(row["low"]), "close": float(row["close"]),
                "tick_volume": int(row.get("tick_volume") or 0),
                "spread": int(row.get("spread") or 0),
            })
    return bars


def resample(bars5, minutes):
    """Aggregate 5m bars into a higher timeframe, aligned to the clock."""
    step = minutes * 60
    buckets = {}
    for b in bars5:
        start = b["time"] - b["time"] % step
        agg = buckets.get(start)
        if agg is None:
            buckets[start] = {"time": start, "open": b["open"], "high": b["high"],
                              "low": b["low"], "close": b["close"],
                              "closed_at": start + step}
        else:
            agg["high"] = max(agg["high"], b["high"])
            agg["low"] = min(agg["low"], b["low"])
            agg["close"] = b["close"]
    return [buckets[k] for k in sorted(buckets)]


def atr(candles, period=14):
    if len(candles) < period + 1:
        return None
    ranges = []
    for prev, cur in zip(candles[-period - 1:-1], candles[-period:]):
        ranges.append(max(cur["high"] - cur["low"],
                          abs(cur["high"] - prev["close"]),
                          abs(cur["low"] - prev["close"])))
    return sum(ranges) / period


def ema_series(values, period):
    out = [None] * len(values)
    if len(values) < period:
        return out
    k = 2.0 / (period + 1)
    current = sum(values[:period]) / period
    out[period - 1] = current
    for i in range(period, len(values)):
        current = values[i] * k + current * (1 - k)
        out[i] = current
    return out


# ============================================
# EXECUTION
# ============================================
def resolve_with_index(bars, start, direction, entry, sl, tp, max_bars):
    """Walk forward from the entry bar. A bar that touches both levels is a loss,
    matching evaluate_journal(). Returns (outcome, R, exit_index)."""
    risk = abs(entry - sl)
    if risk <= 0:
        return "SKIP", 0.0, start
    reward = abs(tp - entry)
    r_mult = reward / risk
    for offset, bar in enumerate(bars[start:start + max_bars]):
        if direction == "LONG":
            if bar["low"] <= sl:
                return "SL", -1.0, start + offset
            if bar["high"] >= tp:
                return "TP", r_mult, start + offset
        else:
            if bar["high"] >= sl:
                return "SL", -1.0, start + offset
            if bar["low"] <= tp:
                return "TP", r_mult, start + offset
    exit_index = min(start + max_bars - 1, len(bars) - 1)
    tail = bars[exit_index]
    move = (tail["close"] - entry) if direction == "LONG" else (entry - tail["close"])
    return "TIMEOUT", move / risk, exit_index


def resolve(bars, start, direction, entry, sl, tp, max_bars):
    outcome, r, _exit = resolve_with_index(
        bars, start, direction, entry, sl, tp, max_bars)
    return outcome, r


def fill_limit(bars, start, direction, level, validity=12):
    """Limit at the order-block edge; returns the fill index or None."""
    for i in range(start, min(start + validity, len(bars))):
        bar = bars[i]
        if direction == "LONG" and bar["low"] <= level:
            return i
        if direction == "SHORT" and bar["high"] >= level:
            return i
    return None


def make_filters(names):
    def rsi_guard(sig, ctx):
        # Do not buy a long that is already overbought, nor sell an oversold short.
        if sig["signal"] == "LONG" and sig["rsi"] >= 70:
            return False
        if sig["signal"] == "SHORT" and sig["rsi"] <= 30:
            return False
        return True

    def sl_cap(sig, ctx):
        # A stop wider than two ATR is a mid-zone entry, not a structural one.
        return ctx["atr"] is not None and sig["sl_dollars"] <= 2.0 * ctx["atr"]

    def ob_edge(sig, ctx):
        # Only take the trade near the protective edge of the order block.
        if not sig.get("ob_high") or not sig.get("ob_low"):
            return True
        span = sig["ob_high"] - sig["ob_low"]
        if span <= 0:
            return True
        pos = (sig["price"] - sig["ob_low"]) / span
        return pos <= 0.5 if sig["signal"] == "LONG" else pos >= 0.5

    def session(sig, ctx):
        # Gold trends in the London/NY overlap; the Asian range chops.
        return 7 <= ctx["hour"] < 17

    def session_7_21(sig, ctx):
        return 7 <= ctx["hour"] < 21

    def no_0407(sig, ctx):
        return ctx["hour"] not in (4, 5, 6)

    def atr4(sig, ctx):
        return ctx["atr"] is not None and ctx["atr"] >= 4.0

    def atr5(sig, ctx):
        return ctx["atr"] is not None and ctx["atr"] >= 5.0

    def short_only(sig, ctx):
        return sig["signal"] == "SHORT"

    def long_only(sig, ctx):
        return sig["signal"] == "LONG"

    def conf_70_79(sig, ctx):
        return 70 <= sig.get("confidence", 0) <= 79

    def conf_max_79(sig, ctx):
        return sig.get("confidence", 0) <= 79

    def htf_ema(sig, ctx):
        fast, slow = ctx.get("ema15_fast"), ctx.get("ema15_slow")
        if fast is None or slow is None:
            return True
        return fast > slow if sig["signal"] == "LONG" else fast < slow

    def htf_ema_1h(sig, ctx):
        fast, slow = ctx.get("ema60_fast"), ctx.get("ema60_slow")
        if fast is None or slow is None:
            return True
        return fast > slow if sig["signal"] == "LONG" else fast < slow

    def extension(sig, ctx):
        # Do not enter a move that is already stretched from its mean.
        if ctx["ema"] is None or ctx["atr"] is None:
            return True
        return abs(sig["price"] - ctx["ema"]) <= 1.5 * ctx["atr"]

    def div_any(sig, ctx):
        return bot.has_divergence(sig)

    def div_regular(sig, ctx):
        return bot.has_divergence(sig, types=("regular",))

    def div_hidden(sig, ctx):
        return bot.has_divergence(sig, types=("hidden",))

    def div_stoch(sig, ctx):
        return bot.has_divergence(sig, oscillators=("stoch_935",))

    def div_stoch_1433(sig, ctx):
        return bot.has_divergence(sig, oscillators=("stoch_1433",))

    def div_rsi(sig, ctx):
        return bot.has_divergence(sig, oscillators=("rsi_",))

    def div_macd(sig, ctx):
        return bot.has_divergence(sig, oscillators=("macd_hist",))

    def div_15m(sig, ctx):
        return bot.has_divergence(sig, timeframe="15m")

    def div_fresh(sig, ctx):
        return bot.has_divergence(sig, max_age=5)

    def div_strong(sig, ctx):
        return bot.has_divergence(sig, min_strength=3.0)

    def div_obv(sig, ctx):
        return bot.has_divergence(sig, oscillators=("obv",))

    def stoch_cross(sig, ctx):
        if sig["signal"] == "LONG":
            return bool(sig.get("stoch_cross_up"))
        return bool(sig.get("stoch_cross_down"))

    def adx_ge_20(sig, ctx):
        return sig.get("adx") is not None and sig["adx"] >= 20.0

    def adx_ge_25(sig, ctx):
        return sig.get("adx") is not None and sig["adx"] >= 25.0

    def adx_lt_20(sig, ctx):
        return sig.get("adx") is not None and sig["adx"] < 20.0

    available = {"rsi": rsi_guard, "sl_cap": sl_cap, "ob_edge": ob_edge,
                 "session": session, "extension": extension,
                 "session_7_21": session_7_21, "no_0407": no_0407,
                 "atr4": atr4, "atr5": atr5, "short": short_only,
                 "long": long_only, "conf_70_79": conf_70_79,
                 "conf_max_79": conf_max_79, "htf_ema": htf_ema,
                 "htf_ema_1h": htf_ema_1h,
                 "div_any": div_any, "div_regular": div_regular,
                 "div_hidden": div_hidden, "div_stoch": div_stoch,
                 "div_stoch_1433": div_stoch_1433, "div_rsi": div_rsi,
                 "div_macd": div_macd, "div_15m": div_15m,
                 "div_fresh": div_fresh, "div_strong": div_strong,
                 "div_obv": div_obv, "stoch_cross": stoch_cross,
                 "adx_20": adx_ge_20, "adx_25": adx_ge_25,
                 "adx_lt20": adx_lt_20}
    return [available[n] for n in names]


def run(bars5, bars15, rr=2.0, filters=(), limit_entry=False, atr_stop=None,
        max_bars=288, cooldown=2700, min_confidence=None, spread=0.0,
        bar_spread=False, pullback_atr=None, pullback_bars=3, delay_bars=0,
        momentum_guard_atr=None, require_choch=False, global_cooldown=0,
        portfolio=False, capital=None, risk_percent=None, daily_loss_r=3.0):
    """Replay the engine bar by bar with no lookahead.

    With ``portfolio=True`` the replay also applies the live account gates: one
    open position at a time, the min-lot risk cap, and the daily loss brake.
    The default stays signal-by-signal so older research remains comparable.
    """
    if min_confidence is not None:
        bot.MIN_CONFIDENCE = min_confidence

    capital = bot.CAPITAL if capital is None else capital
    risk_percent = bot.RISK_PERCENT if risk_percent is None else risk_percent
    risk_cap = capital * risk_percent * bot.MAX_RISK_MULTIPLE
    open_until = -1
    day_r = {}
    risk_rejected = 0

    active = make_filters(filters)
    closed_at = [b["closed_at"] for b in bars15]
    ema15_fast = ema_series([b["close"] for b in bars15], 20)
    ema15_slow = ema_series([b["close"] for b in bars15], 50)
    bars60 = resample(bars15, 60)
    closed60 = [b["closed_at"] for b in bars60]
    ema60_fast = ema_series([b["close"] for b in bars60], 20)
    ema60_slow = ema_series([b["close"] for b in bars60], 50)
    last_sent = {}
    last_any = 0
    trades, generated, unfilled = [], 0, 0

    for i in range(100, len(bars5) - 1):
        now = bars5[i]["time"]
        upto = bisect.bisect_right(closed_at, now)
        upto60 = bisect.bisect_right(closed60, now)
        window15 = bars15[max(0, upto - 100):upto]
        if len(window15) < 20:
            continue

        cache_key = (id(bars5), i, min_confidence)
        if cache_key in _SIGNAL_CACHE:
            sig = _SIGNAL_CACHE[cache_key]
        else:
            with contextlib.redirect_stdout(io.StringIO()):
                sig = bot.generate_signal(bars5[i - 99:i + 1], window15)
            _SIGNAL_CACHE[cache_key] = sig
        if not sig:
            continue
        generated += 1

        day = time.strftime("%Y-%m-%d", time.gmtime(now))
        if portfolio and i <= open_until:
            continue
        if portfolio and day_r.get(day, 0.0) <= -daily_loss_r:
            continue

        direction = "LONG" if sig["signal"] == "LONG" else "SHORT"
        if now - last_sent.get(direction, 0) < cooldown:
            continue
        if global_cooldown and now - last_any < global_cooldown:
            continue

        window = bars5[max(0, i - 60):i + 1]
        ctx = {"atr": atr(window),
               "ema": sum(c["close"] for c in window[-20:]) / 20 if len(window) >= 20 else None,
               "hour": time.gmtime(now).tm_hour,
               "ema15_fast": ema15_fast[upto - 1] if upto else None,
               "ema15_slow": ema15_slow[upto - 1] if upto else None,
               "ema60_fast": ema60_fast[upto60 - 1] if upto60 else None,
               "ema60_slow": ema60_slow[upto60 - 1] if upto60 else None}
        if not all(f(sig, ctx) for f in active):
            continue
        if require_choch and not sig.get("choch"):
            continue
        if momentum_guard_atr and ctx["atr"] and i >= 12:
            move = bars5[i]["close"] - bars5[i - 12]["close"]
            limit = momentum_guard_atr * ctx["atr"]
            if direction == "LONG" and move > limit:
                continue
            if direction == "SHORT" and move < -limit:
                continue

        risk = atr_stop * ctx["atr"] if (atr_stop and ctx["atr"]) else sig["sl_dollars"]
        entry = sig["price"]
        sl = entry - risk if direction == "LONG" else entry + risk
        tp = entry + rr * risk if direction == "LONG" else entry - rr * risk
        start = i + 1

        if limit_entry:
            level = sig["ob_low"] if direction == "LONG" else sig["ob_high"]
            if level is None:
                continue
            fill = fill_limit(bars5, i + 1, direction, level)
            if fill is None:
                unfilled += 1
                last_sent[direction] = now
                last_any = now
                continue
            entry = level
            sl = entry - 1.0 if direction == "LONG" else entry + 1.0
            risk = abs(entry - sl)
            tp = entry + rr * risk if direction == "LONG" else entry - rr * risk
            start = fill + 1
        elif pullback_atr is not None:
            if not ctx["atr"]:
                continue
            offset = pullback_atr * ctx["atr"]
            level = entry - offset if direction == "LONG" else entry + offset
            if (direction == "LONG" and level <= sl) or (direction == "SHORT" and level >= sl):
                continue
            fill = fill_limit(bars5, i + 1, direction, level,
                              validity=pullback_bars)
            if fill is None:
                unfilled += 1
                last_sent[direction] = now
                last_any = now
                continue
            entry = level
            start = fill + 1
        elif delay_bars:
            entry_idx = i + delay_bars
            if entry_idx >= len(bars5) - 1:
                continue
            entry = bars5[entry_idx]["close"]
            sl = entry - risk if direction == "LONG" else entry + risk
            tp = entry + rr * risk if direction == "LONG" else entry - rr * risk
            start = entry_idx + 1

        risk = abs(entry - sl)
        if portfolio and risk > risk_cap:
            risk_rejected += 1
            continue

        outcome, r, exit_idx = resolve_with_index(
            bars5, start, direction, entry, sl, tp, max_bars)
        # Spread and slippage cost a fixed price amount per trade, so a tight
        # stop pays more of it in R terms than a wide one. MT5 bars carry the
        # broker's spread in points (0.01 for XAUUSD); use the entry bar's own
        # spread when asked, otherwise the fixed research assumption.
        cost = spread
        if bar_spread and "spread" in bars5[i]:
            cost = float(bars5[i]["spread"]) * 0.01
        if cost and risk > 0:
            r -= cost / risk
        if portfolio:
            open_until = exit_idx
            exit_day = time.strftime(
                "%Y-%m-%d", time.gmtime(bars5[exit_idx]["time"]))
            day_r[exit_day] = day_r.get(exit_day, 0.0) + r
        last_sent[direction] = now
        last_any = now
        trades.append({"ts": now, "direction": direction, "outcome": outcome,
                       "r": r, "risk": round(abs(entry - sl), 2), "rsi": sig["rsi"],
                       "confidence": sig["confidence"]})

    return summarise(trades, generated, unfilled, risk_rejected)


def summarise(trades, generated, unfilled, risk_rejected=0):
    tp = sum(1 for t in trades if t["outcome"] == "TP")
    sl = sum(1 for t in trades if t["outcome"] == "SL")
    timeout = sum(1 for t in trades if t["outcome"] == "TIMEOUT")
    decided = tp + sl
    total_r = sum(t["r"] for t in trades)
    equity, peak, max_dd = 0.0, 0.0, 0.0
    for t in trades:
        equity += t["r"]
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
    return {
        "generated": generated, "trades": len(trades), "unfilled": unfilled,
        "risk_rejected": risk_rejected,
        "tp": tp, "sl": sl, "timeout": timeout,
        "wr": round(100 * tp / decided) if decided else None,
        "expectancy": round(total_r / len(trades), 3) if trades else 0.0,
        "total_r": round(total_r, 2), "max_dd": round(max_dd, 2),
        "avg_risk": round(sum(t["risk"] for t in trades) / len(trades), 2) if trades else 0.0,
        "long": sum(1 for t in trades if t["direction"] == "LONG"),
        "short": sum(1 for t in trades if t["direction"] == "SHORT"),
    }


VARIANTS = [
    ("baseline (market, 1:2)", dict(rr=2.0)),
    ("market, 1:1.5", dict(rr=1.5)),
    ("market, 1:3", dict(rr=3.0)),
    ("+ RSI guard", dict(rr=2.0, filters=("rsi",))),
    ("+ SL <= 2 ATR", dict(rr=2.0, filters=("sl_cap",))),
    ("+ entry near OB edge", dict(rr=2.0, filters=("ob_edge",))),
    ("+ RSI + SL cap", dict(rr=2.0, filters=("rsi", "sl_cap"))),
    ("+ session 07-17 UTC", dict(rr=2.0, filters=("session",))),
    ("+ near EMA20", dict(rr=2.0, filters=("extension",))),
    ("+ RSI + session", dict(rr=2.0, filters=("rsi", "session"))),
    ("ATR stop 1.0x, 1:2", dict(rr=2.0, atr_stop=1.0)),
    ("ATR stop 1.5x, 1:2", dict(rr=2.0, atr_stop=1.5)),
    ("ATR stop 2.0x, 1:2", dict(rr=2.0, atr_stop=2.0)),
    ("ATR stop 1.5x, 1:3", dict(rr=3.0, atr_stop=1.5)),
    ("ATR 1.5x, 1:3, session", dict(rr=3.0, atr_stop=1.5, filters=("session",))),
    ("ATR 1.5x, 1:3, sess+EMA", dict(rr=3.0, atr_stop=1.5,
                                     filters=("session", "extension"))),
    ("limit at OB edge", dict(rr=2.0, limit_entry=True)),
    ("limit + RSI guard", dict(rr=2.0, limit_entry=True, filters=("rsi",))),
    # P2 entry-timing experiments. These are research-only; live defaults are
    # unchanged until the shadow sample earns a promotion.
    ("pullback 0.25 ATR", dict(rr=2.0, pullback_atr=0.25, pullback_bars=3)),
    ("pullback 0.50 ATR", dict(rr=2.0, pullback_atr=0.50, pullback_bars=3)),
    ("delay 1 bar", dict(rr=2.0, delay_bars=1)),
    ("momentum guard 2ATR", dict(rr=2.0, momentum_guard_atr=2.0)),
    ("CHoCH only", dict(rr=2.0, require_choch=True)),
    # Regime filters: these are the closest thing to a safe "overhaul" because
    # they only remove trades from the existing engine.
    ("+ ATR >= 4", dict(rr=2.0, filters=("atr4",))),
    ("+ ATR >= 5", dict(rr=2.0, filters=("atr5",))),
    ("+ session 07-21", dict(rr=2.0, filters=("session_7_21",))),
    ("+ no 04-07 UTC", dict(rr=2.0, filters=("no_0407",))),
    ("+ ATR4 + session 07-21", dict(rr=2.0, filters=("atr4", "session_7_21"))),
    ("+ short only", dict(rr=2.0, filters=("short",))),
    ("+ short + ATR4", dict(rr=2.0, filters=("short", "atr4"))),
    ("+ long only", dict(rr=2.0, filters=("long",))),
    ("+ long + ATR4", dict(rr=2.0, filters=("long", "atr4"))),
    ("+ conf 70-79", dict(rr=2.0, filters=("conf_70_79",))),
    ("+ conf <= 79", dict(rr=2.0, filters=("conf_max_79",))),
    ("+ 15M EMA trend", dict(rr=2.0, filters=("htf_ema",))),
    ("+ 15M EMA + ATR4", dict(rr=2.0, filters=("htf_ema", "atr4"))),
    ("+ 15M EMA + short", dict(rr=2.0, filters=("htf_ema", "short"))),
    ("+ 1H EMA", dict(rr=2.0, filters=("htf_ema_1h",))),
    ("+ 1H EMA + ATR4", dict(rr=2.0, filters=("htf_ema_1h", "atr4"))),
    # Divergence research. The signal engine attaches all recent regular and
    # hidden pairs; these variants test which subset actually survives costs.
    ("+ div any", dict(rr=2.0, filters=("div_any",))),
    ("+ div regular", dict(rr=2.0, filters=("div_regular",))),
    ("+ div hidden", dict(rr=2.0, filters=("div_hidden",))),
    ("+ div stoch 9,3,5", dict(rr=2.0, filters=("div_stoch",))),
    ("+ div stoch 14,3,3", dict(rr=2.0, filters=("div_stoch_1433",))),
    ("+ div RSI", dict(rr=2.0, filters=("div_rsi",))),
    ("+ div MACD", dict(rr=2.0, filters=("div_macd",))),
    ("+ div hidden + ATR4", dict(rr=2.0, filters=("div_hidden", "atr4"))),
    ("+ div hidden + session", dict(rr=2.0, filters=("div_hidden", "session"))),
    ("+ div any + ATR4", dict(rr=2.0, filters=("div_any", "atr4"))),
    ("+ div any + 15M EMA", dict(rr=2.0, filters=("div_any", "htf_ema"))),
    # Combinations. These are deliberately few: each one adds a filter that was
    # independently useful, rather than searching every pair in the matrix.
    ("ATR2 + OB edge", dict(rr=2.0, atr_stop=2.0,
                            filters=("ob_edge",))),
    ("ATR2 + OB + no0407", dict(rr=2.0, atr_stop=2.0,
                                filters=("ob_edge", "no_0407"))),
    ("ATR2 + OB + momentum", dict(rr=2.0, atr_stop=2.0,
                                  filters=("ob_edge",),
                                  momentum_guard_atr=2.0)),
    ("ATR2 + OB + div hidden", dict(rr=2.0, atr_stop=2.0,
                                    filters=("ob_edge", "div_hidden"))),
    ("ATR2 + OB + div regular", dict(rr=2.0, atr_stop=2.0,
                                     filters=("ob_edge", "div_regular"))),
    ("ATR2 + div hidden", dict(rr=2.0, atr_stop=2.0,
                               filters=("div_hidden",))),
    ("OB edge + no0407", dict(rr=2.0,
                              filters=("ob_edge", "no_0407"))),
    ("OB edge + 15M EMA", dict(rr=2.0,
                               filters=("ob_edge", "htf_ema"))),
    ("OB edge + 15M EMA + div hidden", dict(
        rr=2.0, filters=("ob_edge", "htf_ema", "div_hidden"))),
    ("OB edge + 15M EMA + div hidden + ATR4", dict(
        rr=2.0, filters=("ob_edge", "htf_ema", "div_hidden", "atr4"))),
    ("OB edge + 15M EMA + div regular", dict(
        rr=2.0, filters=("ob_edge", "htf_ema", "div_regular"))),
    ("ATR2 + OB + 15M EMA", dict(
        rr=2.0, atr_stop=2.0, filters=("ob_edge", "htf_ema"))),
    ("ATR2 + OB + 15M EMA + div hidden", dict(
        rr=2.0, atr_stop=2.0,
        filters=("ob_edge", "htf_ema", "div_hidden"))),
    ("short + ATR2 + OB", dict(rr=2.0, atr_stop=2.0,
                               filters=("short", "ob_edge"))),
    ("short + 15M EMA + ATR2 + OB", dict(
        rr=2.0, atr_stop=2.0,
        filters=("short", "htf_ema", "ob_edge"))),
    # New research from the divergence literature: MTF confirmation, ADX
    # strength, a Stochastic trigger, volume and freshness.
    ("+ div 15M", dict(rr=2.0, filters=("div_15m",))),
    ("+ div fresh", dict(rr=2.0, filters=("div_fresh",))),
    ("+ div strong", dict(rr=2.0, filters=("div_strong",))),
    ("+ div OBV", dict(rr=2.0, filters=("div_obv",))),
    ("+ stoch cross", dict(rr=2.0, filters=("stoch_cross",))),
    ("+ ADX >= 20", dict(rr=2.0, filters=("adx_20",))),
    ("+ ADX >= 25", dict(rr=2.0, filters=("adx_25",))),
    ("+ ADX < 20", dict(rr=2.0, filters=("adx_lt20",))),
    ("OB+EMA+div+ATR4+15M div", dict(
        rr=2.0, filters=("ob_edge", "htf_ema", "div_hidden", "atr4",
                         "div_15m"))),
    ("OB+EMA+div+ATR4+ADX20", dict(
        rr=2.0, filters=("ob_edge", "htf_ema", "div_hidden", "atr4",
                         "adx_20"))),
    ("OB+EMA+div+ATR4+ADX25", dict(
        rr=2.0, filters=("ob_edge", "htf_ema", "div_hidden", "atr4",
                         "adx_25"))),
    ("OB+EMA+div+ATR4+stoch", dict(
        rr=2.0, filters=("ob_edge", "htf_ema", "div_hidden", "atr4",
                         "stoch_cross"))),
    ("OB+EMA+div+ATR4+OBV", dict(
        rr=2.0, filters=("ob_edge", "htf_ema", "div_hidden", "atr4",
                         "div_obv"))),
    ("OB+EMA+div+ATR4+fresh", dict(
        rr=2.0, filters=("ob_edge", "htf_ema", "div_hidden", "atr4",
                         "div_fresh"))),
    ("OB+EMA+div+ATR4+strong", dict(
        rr=2.0, filters=("ob_edge", "htf_ema", "div_hidden", "atr4",
                         "div_strong"))),
    ("OB+div+ATR4+1H EMA", dict(
        rr=2.0, filters=("ob_edge", "div_hidden", "atr4",
                         "htf_ema_1h"))),
    ("OB+div+ATR4+15M+1H EMA", dict(
        rr=2.0, filters=("ob_edge", "div_hidden", "atr4",
                         "htf_ema", "htf_ema_1h"))),
    ("OB+EMA+div+ATR4 1:1.5", dict(
        rr=1.5, filters=("ob_edge", "htf_ema", "div_hidden", "atr4"))),
    ("OB+EMA+div+ATR4 1:3", dict(
        rr=3.0, filters=("ob_edge", "htf_ema", "div_hidden", "atr4"))),
    ("global cooldown 45m", dict(rr=2.0, global_cooldown=2700)),
]


def _date_bound(value, add_day=False):
    parsed = datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return parsed + (timedelta(days=1) if add_day else timedelta())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--data", choices=("auto", "yahoo", "mt5"), default="auto",
                    help="auto uses MT5 when MT5_API_KEY is set")
    ap.add_argument("--min-conf", type=int, default=None)
    ap.add_argument("--max-bars", type=int, default=288)
    ap.add_argument("--start", help="first UTC date, YYYY-MM-DD")
    ap.add_argument("--end", help="last UTC date, YYYY-MM-DD")
    ap.add_argument("--spread", type=float, default=0.0,
                    help="round-trip cost in $/oz, applied per trade")
    ap.add_argument("--bar-spread", action="store_true",
                    help="use each MT5 entry bar's own spread instead of --spread")
    ap.add_argument("--portfolio", action="store_true",
                    help="apply max-one-position, min-lot risk cap and daily loss brake")
    ap.add_argument("--capital", type=float, default=None,
                    help="account capital for the min-lot risk cap")
    ap.add_argument("--risk-percent", type=float, default=None,
                    help="risk per trade for the min-lot risk cap")
    ap.add_argument("--daily-loss-r", type=float, default=3.0,
                    help="daily loss brake in R (portfolio mode)")
    ap.add_argument("--only", help="semicolon-separated variant name filters")
    args = ap.parse_args()

    data_source = args.data
    if data_source == "auto":
        data_source = "mt5" if mt5_data.mt5_configured() else "yahoo"

    if data_source == "mt5":
        fetch_end = _date_bound(args.end, add_day=True) if args.end \
            else datetime.now(timezone.utc)
        fetch_start = _date_bound(args.start) if args.start \
            else fetch_end - timedelta(days=180)
        bars5 = load_bars_mt5(fetch_start, fetch_end, args.refresh)
        source_label = "MT5 XAUUSD"
    else:
        bars5 = load_bars(args.refresh)
        source_label = "Yahoo GC=F"

    if args.start:
        cutoff = calendar.timegm(time.strptime(args.start, "%Y-%m-%d"))
        bars5 = [b for b in bars5 if b["time"] >= cutoff]
    if args.end:
        cutoff = calendar.timegm(time.strptime(args.end, "%Y-%m-%d")) + 86400
        bars5 = [b for b in bars5 if b["time"] < cutoff]
    bars15 = resample(bars5, 15)
    _SIGNAL_CACHE.clear()
    span = (time.strftime("%Y-%m-%d", time.gmtime(bars5[0]["time"])),
            time.strftime("%Y-%m-%d", time.gmtime(bars5[-1]["time"])))
    print(f"{len(bars5)} 5m bars | {span[0]} -> {span[1]} | "
          f"{len(bars15)} 15m bars | source={source_label} | min_conf="
          f"{args.min_conf if args.min_conf is not None else bot.MIN_CONFIDENCE}\n")

    header = (f"{'variant':26} {'gen':>4} {'trd':>4} {'nofl':>4} {'rej':>4} "
              f"{'TP':>4} {'SL':>4} {'TO':>4} {'WR%':>5} {'E[R]':>6} "
              f"{'totR':>7} {'DD':>5} {'avgSL':>6}")
    print(header)
    print("-" * len(header))
    if args.bar_spread:
        print("cost model: each entry bar's MT5 spread")
    else:
        print(f"cost model: ${args.spread:.2f}/oz per trade")
    if args.portfolio:
        capital = args.capital if args.capital is not None else bot.CAPITAL
        risk_percent = (args.risk_percent if args.risk_percent is not None
                        else bot.RISK_PERCENT)
        print(f"portfolio gates: capital=${capital:.2f} risk={risk_percent:.2%} "
              f"max_open=1 daily_stop={args.daily_loss_r:g}R")
    print()
    variants = [v for v in VARIANTS if not args.only or
                any(k.strip().lower() in v[0].lower()
                    for k in args.only.split(";"))]
    for name, cfg in variants:
        result = run(bars5, bars15, max_bars=args.max_bars,
                     min_confidence=args.min_conf, spread=args.spread,
                     bar_spread=args.bar_spread,
                     portfolio=args.portfolio, capital=args.capital,
                     risk_percent=args.risk_percent,
                     daily_loss_r=args.daily_loss_r, **cfg)
        wr = "-" if result["wr"] is None else result["wr"]
        print(f"{name:26} {result['generated']:>4} {result['trades']:>4} "
              f"{result['unfilled']:>4} {result['risk_rejected']:>4} "
              f"{result['tp']:>4} {result['sl']:>4} "
              f"{result['timeout']:>4} {str(wr):>5} {result['expectancy']:>6} "
              f"{result['total_r']:>7} {result['max_dd']:>5} {result['avg_risk']:>6}")


if __name__ == "__main__":
    main()
