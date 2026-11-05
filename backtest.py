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
from pathlib import Path

import bot

DATA_DIR = Path(__file__).parent / "backtest_data"
YAHOO = ("https://query1.finance.yahoo.com/v8/finance/chart/"
         "{symbol}?interval=5m&range={rng}")

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


# ============================================
# EXECUTION
# ============================================
def resolve(bars, start, direction, entry, sl, tp, max_bars):
    """Walk forward from the entry bar. A bar that touches both levels is a loss,
    matching evaluate_journal(). Returns (outcome, R)."""
    risk = abs(entry - sl)
    if risk <= 0:
        return "SKIP", 0.0
    reward = abs(tp - entry)
    r_mult = reward / risk
    for bar in bars[start:start + max_bars]:
        if direction == "LONG":
            if bar["low"] <= sl:
                return "SL", -1.0
            if bar["high"] >= tp:
                return "TP", r_mult
        else:
            if bar["high"] >= sl:
                return "SL", -1.0
            if bar["low"] <= tp:
                return "TP", r_mult
    tail = bars[start + max_bars - 1] if start + max_bars - 1 < len(bars) else bars[-1]
    move = (tail["close"] - entry) if direction == "LONG" else (entry - tail["close"])
    return "TIMEOUT", move / risk


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

    def extension(sig, ctx):
        # Do not enter a move that is already stretched from its mean.
        if ctx["ema"] is None or ctx["atr"] is None:
            return True
        return abs(sig["price"] - ctx["ema"]) <= 1.5 * ctx["atr"]

    available = {"rsi": rsi_guard, "sl_cap": sl_cap, "ob_edge": ob_edge,
                 "session": session, "extension": extension}
    return [available[n] for n in names]


def run(bars5, bars15, rr=2.0, filters=(), limit_entry=False, atr_stop=None,
        max_bars=288, cooldown=2700, min_confidence=None, spread=0.0):
    """Replay the engine bar by bar with no lookahead."""
    if min_confidence is not None:
        bot.MIN_CONFIDENCE = min_confidence

    active = make_filters(filters)
    closed_at = [b["closed_at"] for b in bars15]
    last_sent = {}
    trades, generated, unfilled = [], 0, 0

    for i in range(100, len(bars5) - 1):
        now = bars5[i]["time"]
        upto = bisect.bisect_right(closed_at, now)
        window15 = bars15[max(0, upto - 100):upto]
        if len(window15) < 20:
            continue

        with contextlib.redirect_stdout(io.StringIO()):
            sig = bot.generate_signal(bars5[i - 99:i + 1], window15)
        if not sig:
            continue
        generated += 1

        direction = "LONG" if sig["signal"] == "LONG" else "SHORT"
        if now - last_sent.get(direction, 0) < cooldown:
            continue

        window = bars5[max(0, i - 60):i + 1]
        ctx = {"atr": atr(window),
               "ema": sum(c["close"] for c in window[-20:]) / 20 if len(window) >= 20 else None,
               "hour": time.gmtime(now).tm_hour}
        if not all(f(sig, ctx) for f in active):
            continue

        risk = atr_stop * ctx["atr"] if (atr_stop and ctx["atr"]) else sig["sl_dollars"]
        if limit_entry:
            level = sig["ob_low"] if direction == "LONG" else sig["ob_high"]
            if level is None:
                continue
            fill = fill_limit(bars5, i + 1, direction, level)
            if fill is None:
                unfilled += 1
                last_sent[direction] = now
                continue
            entry = level
            sl = entry - 1.0 if direction == "LONG" else entry + 1.0
            risk = abs(entry - sl)
            tp = entry + rr * risk if direction == "LONG" else entry - rr * risk
            start = fill + 1
        else:
            entry = sig["price"]
            sl = entry - risk if direction == "LONG" else entry + risk
            tp = entry + rr * risk if direction == "LONG" else entry - rr * risk
            start = i + 1

        outcome, r = resolve(bars5, start, direction, entry, sl, tp, max_bars)
        # Spread and slippage cost a fixed price amount per trade, so a tight
        # stop pays more of it in R terms than a wide one.
        if spread and risk > 0:
            r -= spread / risk
        last_sent[direction] = now
        trades.append({"ts": now, "direction": direction, "outcome": outcome,
                       "r": r, "risk": round(risk, 2), "rsi": sig["rsi"],
                       "confidence": sig["confidence"]})

    return summarise(trades, generated, unfilled)


def summarise(trades, generated, unfilled):
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
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--min-conf", type=int, default=None)
    ap.add_argument("--max-bars", type=int, default=288)
    ap.add_argument("--start", help="first UTC date, YYYY-MM-DD")
    ap.add_argument("--end", help="last UTC date, YYYY-MM-DD")
    ap.add_argument("--spread", type=float, default=0.0,
                    help="round-trip cost in $/oz, applied per trade")
    ap.add_argument("--only", help="semicolon-separated variant name filters")
    args = ap.parse_args()

    bars5 = load_bars(args.refresh)
    if args.start:
        cutoff = calendar.timegm(time.strptime(args.start, "%Y-%m-%d"))
        bars5 = [b for b in bars5 if b["time"] >= cutoff]
    if args.end:
        cutoff = calendar.timegm(time.strptime(args.end, "%Y-%m-%d")) + 86400
        bars5 = [b for b in bars5 if b["time"] < cutoff]
    bars15 = resample(bars5, 15)
    span = (time.strftime("%Y-%m-%d", time.gmtime(bars5[0]["time"])),
            time.strftime("%Y-%m-%d", time.gmtime(bars5[-1]["time"])))
    print(f"{len(bars5)} 5m bars | {span[0]} -> {span[1]} | "
          f"{len(bars15)} 15m bars | min_conf="
          f"{args.min_conf if args.min_conf is not None else bot.MIN_CONFIDENCE}\n")

    header = (f"{'variant':26} {'gen':>4} {'trd':>4} {'nofl':>4} {'TP':>4} {'SL':>4} "
              f"{'TO':>4} {'WR%':>5} {'E[R]':>6} {'totR':>7} {'DD':>5} {'avgSL':>6}")
    print(header)
    print("-" * len(header))
    print(f"cost model: ${args.spread:.2f}/oz per trade\n")
    variants = [v for v in VARIANTS if not args.only or
                any(k.strip().lower() in v[0].lower()
                    for k in args.only.split(";"))]
    for name, cfg in variants:
        result = run(bars5, bars15, max_bars=args.max_bars,
                     min_confidence=args.min_conf, spread=args.spread, **cfg)
        wr = "-" if result["wr"] is None else result["wr"]
        print(f"{name:26} {result['generated']:>4} {result['trades']:>4} "
              f"{result['unfilled']:>4} {result['tp']:>4} {result['sl']:>4} "
              f"{result['timeout']:>4} {str(wr):>5} {result['expectancy']:>6} "
              f"{result['total_r']:>7} {result['max_dd']:>5} {result['avg_risk']:>6}")


if __name__ == "__main__":
    main()
