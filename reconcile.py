#!/usr/bin/env python3
"""Re-resolve journal records from stored closed bars.

The live bot used to evaluate the still-forming 5m candle and then advance
`last_bar`, so a TP/SL touched later in that same candle could be missed. This
tool rebuilds each record's first touch from the `bars` table. It is read-only
unless `--apply` is passed.

Usage:
    TURSO_DATABASE_URL=... TURSO_AUTH_TOKEN=... python reconcile.py
    TURSO_DATABASE_URL=... TURSO_AUTH_TOKEN=... python reconcile.py --apply
"""

import argparse
import sys
import time

import bot


def load_bars():
    rows = bot.db_run([(
        "SELECT bar_time, open, high, low, close FROM bars "
        "WHERE symbol=? AND timeframe='5min' ORDER BY bar_time",
        (bot.SYMBOL,),
    )], fetch=True)
    bars = [
        {"time": row[0], "open": row[1], "high": row[2], "low": row[3], "close": row[4]}
        for row in rows
    ]
    return bot.closed_candles(bars, "5min", now=time.time())


def rebuild(rec, bars):
    """Return the first touch from the entry bar onward, or None if unverifiable."""
    entry_bar = rec.get("entry_bar")
    entry, stop, take = rec.get("entry"), rec.get("sl"), rec.get("tp")
    if not entry_bar or entry is None or stop is None or take is None:
        return None
    risk = abs(entry - stop)
    if risk <= 0:
        return None

    long = rec.get("direction") in ("BUY", "LONG")
    mfe = float(rec.get("mfe") or 0.0)
    mae = float(rec.get("mae") or 0.0)
    last = None
    for candle in bars:
        if candle["time"] <= entry_bar:
            continue
        last = candle["time"]
        if long:
            mfe = max(mfe, (candle["high"] - entry) / risk)
            mae = min(mae, (candle["low"] - entry) / risk)
            stop_hit = candle["low"] <= stop
            take_hit = candle["high"] >= take
        else:
            mfe = max(mfe, (entry - candle["low"]) / risk)
            mae = min(mae, (entry - candle["high"]) / risk)
            stop_hit = candle["high"] >= stop
            take_hit = candle["low"] <= take
        if stop_hit or take_hit:
            outcome = "SL" if stop_hit else "TP"
            return {
                "outcome": outcome,
                "last_bar": last,
                "closed_ts": bot.bar_close_ts(last, 5),
                "mfe": round(mfe, 4),
                "mae": round(mae, 4),
            }
    return {
        "outcome": "OPEN",
        "last_bar": last,
        "closed_ts": None,
        "mfe": round(mfe, 4),
        "mae": round(mae, 4),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write the rebuilt outcomes")
    args = parser.parse_args()

    bot.db_init()
    journal = bot.db_load_journal()
    bars = load_bars()
    if not bars:
        print("No 5m bars stored; nothing to reconcile.")
        return 1
    if not journal:
        print("Journal is empty; nothing to reconcile.")
        return 0

    changed = 0
    unverifiable = 0
    for rec in journal:
        rebuilt = rebuild(rec, bars)
        if rebuilt is None:
            unverifiable += 1
            print(f"- {rec['ts']:.0f} {rec['direction']:5} "
                  f"{rec.get('outcome'):4} -> unverifiable (no entry_bar)")
            continue
        if not rec.get("risk_usd"):
            risk = abs((rec.get("entry") or 0) - (rec.get("sl") or 0))
            rec["risk_usd"] = bot.calculate_lots(risk)[1] if risk > 0 else None
        if not rec.get("entry_ts"):
            rec["entry_ts"] = rec["ts"]

        before = rec.get("outcome")
        if before != rebuilt["outcome"]:
            changed += 1
            stamp = rec.get("closed_ts")
            stamp_txt = (time.strftime("%m-%d %H:%M", time.gmtime(stamp))
                         if stamp else "-")
            print(f"{rec['ts']:.3f} {rec['direction']:5} {before:4} -> "
                  f"{rebuilt['outcome']:4} | entry {rec.get('entry')} "
                  f"| was closed {stamp_txt} | first touch {rebuilt['last_bar']}")
            rec.update(rebuilt)
            if args.apply:
                bot.db_update_signal(rec)

    print(f"\n{len(journal)} records | {changed} outcome change(s) | "
          f"{unverifiable} unverifiable")
    if args.apply:
        print("Applied to the database.")
    else:
        print("Dry run only. Re-run with --apply to write these changes.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
