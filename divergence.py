"""Stochastic / RSI / MACD divergence detection.

The detector is deliberately separate from the SMC engine.  It takes candles,
returns plain dictionaries, and never sends or scores a signal by itself.  That
keeps the same code usable by the live bot, the shadow logger and the backtest,
and it makes the "no lookahead" rule testable in isolation.

A pivot at index ``i`` is only returned once ``right`` bars after it have
closed.  A divergence is only considered while the pivot is still recent
(``max_age``).  Both rules are what stop a backtest from buying a swing low that
the live bot could not have known about yet.
"""

from __future__ import annotations

import math


# ---------------------------------------------------------------------------
# Indicator series
# ---------------------------------------------------------------------------
def sma_series(values, period):
    """Simple moving average; None until a full window of real values exists."""
    out = [None] * len(values)
    window = []
    total = 0.0
    for index, value in enumerate(values):
        if value is None:
            window = []
            total = 0.0
            continue
        window.append(value)
        total += value
        if len(window) > period:
            total -= window.pop(0)
        if len(window) == period:
            out[index] = total / period
    return out


def ema_series(values, period):
    """EMA that tolerates leading Nones (used for MACD's signal line)."""
    out = [None] * len(values)
    valid = [i for i, value in enumerate(values) if value is not None]
    if len(valid) < period:
        return out
    start = valid[period - 1]
    current = sum(values[i] for i in valid[:period]) / period
    out[start] = current
    multiplier = 2.0 / (period + 1)
    for index in range(start + 1, len(values)):
        value = values[index]
        if value is None:
            continue
        current = value * multiplier + current * (1.0 - multiplier)
        out[index] = current
    return out


def rsi_series(candles, period=14):
    """Wilder RSI. The live bot's single-value RSI uses a simple average; this
    series is the standard one, which is what divergence comparisons expect."""
    closes = [c["close"] for c in candles]
    out = [None] * len(closes)
    if len(closes) < period + 1:
        return out

    gains = losses = 0.0
    for index in range(1, period + 1):
        delta = closes[index] - closes[index - 1]
        gains += max(delta, 0.0)
        losses += max(-delta, 0.0)
    avg_gain = gains / period
    avg_loss = losses / period

    def value(gain, loss):
        if loss == 0:
            return 100.0
        return 100.0 - 100.0 / (1.0 + gain / loss)

    out[period] = value(avg_gain, avg_loss)
    for index in range(period + 1, len(closes)):
        delta = closes[index] - closes[index - 1]
        avg_gain = (avg_gain * (period - 1) + max(delta, 0.0)) / period
        avg_loss = (avg_loss * (period - 1) + max(-delta, 0.0)) / period
        out[index] = value(avg_gain, avg_loss)
    return out


def stoch_series(candles, k=9, d=3, smooth=5):
    """Slow stochastic: raw %K over ``k``, SMA ``smooth``, then %D over ``d``."""
    raw = [None] * len(candles)
    for index in range(k - 1, len(candles)):
        window = candles[index - k + 1:index + 1]
        highest = max(c["high"] for c in window)
        lowest = min(c["low"] for c in window)
        if highest == lowest:
            raw[index] = 50.0
        else:
            raw[index] = 100.0 * (candles[index]["close"] - lowest) / (
                highest - lowest)
    k_line = sma_series(raw, smooth)
    d_line = sma_series(k_line, d)
    return k_line, d_line


def macd_series(candles, fast=12, slow=26, signal=9):
    closes = [c["close"] for c in candles]
    fast_line = ema_series(closes, fast)
    slow_line = ema_series(closes, slow)
    macd = [None if (f is None or s is None) else f - s
            for f, s in zip(fast_line, slow_line)]
    signal_line = ema_series(macd, signal)
    hist = [None if (m is None or s is None) else m - s
            for m, s in zip(macd, signal_line)]
    return macd, signal_line, hist


def obv_series(candles):
    """On-balance volume when the feed carries volume; None otherwise."""
    volume_key = None
    for key in ("tick_volume", "volume", "real_volume"):
        if candles and key in candles[0]:
            volume_key = key
            break
    if not volume_key:
        return None

    out = [0.0] * len(candles)
    current = 0.0
    for index in range(1, len(candles)):
        previous = candles[index - 1]["close"]
        close = candles[index]["close"]
        volume = float(candles[index].get(volume_key) or 0.0)
        if close > previous:
            current += volume
        elif close < previous:
            current -= volume
        out[index] = current
    return out


def atr(candles, period=14):
    if len(candles) < period + 1:
        return None
    ranges = []
    for previous, current in zip(candles[-period - 1:-1], candles[-period:]):
        ranges.append(max(current["high"] - current["low"],
                          abs(current["high"] - previous["close"]),
                          abs(current["low"] - previous["close"])))
    return sum(ranges) / period


def adx_series(candles, period=14):
    """Wilder ADX. Direction-agnostic: it measures trend strength, not side."""
    size = len(candles)
    out = [None] * size
    if size < period * 2 + 1:
        return out

    tr = [0.0] * size
    plus_dm = [0.0] * size
    minus_dm = [0.0] * size
    for index in range(1, size):
        current = candles[index]
        previous = candles[index - 1]
        tr[index] = max(
            current["high"] - current["low"],
            abs(current["high"] - previous["close"]),
            abs(current["low"] - previous["close"]),
        )
        up = current["high"] - previous["high"]
        down = previous["low"] - current["low"]
        plus_dm[index] = up if (up > down and up > 0) else 0.0
        minus_dm[index] = down if (down > up and down > 0) else 0.0

    tr_s = [None] * size
    plus_s = [None] * size
    minus_s = [None] * size
    tr_s[period] = sum(tr[1:period + 1])
    plus_s[period] = sum(plus_dm[1:period + 1])
    minus_s[period] = sum(minus_dm[1:period + 1])
    for index in range(period + 1, size):
        tr_s[index] = tr_s[index - 1] - tr_s[index - 1] / period + tr[index]
        plus_s[index] = plus_s[index - 1] - plus_s[index - 1] / period + plus_dm[index]
        minus_s[index] = minus_s[index - 1] - minus_s[index - 1] / period + minus_dm[index]

    dx = [None] * size
    for index in range(period, size):
        if not tr_s[index]:
            continue
        di_plus = 100.0 * plus_s[index] / tr_s[index]
        di_minus = 100.0 * minus_s[index] / tr_s[index]
        total = di_plus + di_minus
        dx[index] = 100.0 * abs(di_plus - di_minus) / total if total else 0.0

    first = period * 2 - 1
    if first < size:
        seed = [dx[i] for i in range(period, first + 1) if dx[i] is not None]
        if len(seed) == period:
            out[first] = sum(seed) / period
            for index in range(first + 1, size):
                if dx[index] is None or out[index - 1] is None:
                    continue
                out[index] = (out[index - 1] * (period - 1) + dx[index]) / period
    return out


def adx(candles, period=14):
    series = adx_series(candles, period)
    return series[-1] if series and series[-1] is not None else None


# ---------------------------------------------------------------------------
# Pivots
# ---------------------------------------------------------------------------
def pivot_lows(candles, left=2, right=2):
    """Indices of confirmed swing lows; the last ``right`` bars cannot qualify."""
    pivots = []
    for index in range(left, len(candles) - right):
        low = candles[index]["low"]
        if all(low <= candles[index - offset]["low"]
               for offset in range(1, left + 1)) and \
           all(low < candles[index + offset]["low"]
               for offset in range(1, right + 1)):
            pivots.append(index)
    return pivots


def pivot_highs(candles, left=2, right=2):
    pivots = []
    for index in range(left, len(candles) - right):
        high = candles[index]["high"]
        if all(high >= candles[index - offset]["high"]
               for offset in range(1, left + 1)) and \
           all(high > candles[index + offset]["high"]
               for offset in range(1, right + 1)):
            pivots.append(index)
    return pivots


# ---------------------------------------------------------------------------
# Divergence
# ---------------------------------------------------------------------------
def _detect_pair(candles, series, oscillator, direction, kind, atr_value,
                 left, right, min_sep, max_sep, max_age, min_price_atr,
                 min_osc_delta, zone_low, zone_high):
    if series is None:
        return None
    pivots = pivot_lows(candles, left, right) if direction == "LONG" \
        else pivot_highs(candles, left, right)
    valid = [i for i in pivots if series[i] is not None]
    if len(valid) < 2:
        return None

    # Consecutive pivots are not the only pair that can diverge. In a trend the
    # last two swings often move together, while the meaningful comparison is
    # the last swing against the one before it. Walk the recent pivot list from
    # newest to oldest and return the freshest pair that actually diverges.
    for current_pos in range(len(valid) - 1, 0, -1):
        current = valid[current_pos]
        age = len(candles) - 1 - current
        if age < 0 or age > max_age:
            continue

        for prev_pos in range(current_pos - 1, -1, -1):
            previous = valid[prev_pos]
            separation = current - previous
            if separation < min_sep:
                continue
            if separation > max_sep:
                break

            if direction == "LONG":
                price_prev = candles[previous]["low"]
                price_curr = candles[current]["low"]
            else:
                price_prev = candles[previous]["high"]
                price_curr = candles[current]["high"]

            osc_prev = series[previous]
            osc_curr = series[current]

            if kind == "regular":
                price_ok = price_curr < price_prev if direction == "LONG" \
                    else price_curr > price_prev
                osc_ok = osc_curr > osc_prev if direction == "LONG" \
                    else osc_curr < osc_prev
                osc_delta = (osc_curr - osc_prev) if direction == "LONG" \
                    else (osc_prev - osc_curr)
            else:  # hidden continuation
                price_ok = price_curr > price_prev if direction == "LONG" \
                    else price_curr < price_prev
                osc_ok = osc_curr < osc_prev if direction == "LONG" \
                    else osc_curr > osc_prev
                osc_delta = (osc_prev - osc_curr) if direction == "LONG" \
                    else (osc_curr - osc_prev)

            if not price_ok or not osc_ok or osc_delta < min_osc_delta:
                continue

            price_delta = abs(price_curr - price_prev)
            if atr_value and price_delta < min_price_atr * atr_value:
                continue
            if not atr_value and price_delta < 0.30:
                continue

            if kind == "regular":
                if direction == "LONG" and zone_low is not None:
                    if min(osc_prev, osc_curr) > zone_low:
                        continue
                if direction == "SHORT" and zone_high is not None:
                    if max(osc_prev, osc_curr) < zone_high:
                        continue

            strength = 1.0
            strength += min(2.0, osc_delta / max(min_osc_delta, 1e-9))
            if atr_value:
                strength += min(1.0, price_delta / max(atr_value * 1.5, 1e-9))
            strength = round(min(strength, 4.0), 2)

            return {
                "type": f"{kind}_{'bullish' if direction == 'LONG' else 'bearish'}",
                "direction": direction,
                "kind": kind,
                "oscillator": oscillator,
                "prev_idx": previous,
                "curr_idx": current,
                "age": age,
                "separation": separation,
                "price_prev": round(price_prev, 4),
                "price_curr": round(price_curr, 4),
                "osc_prev": round(osc_prev, 4),
                "osc_curr": round(osc_curr, 4),
                "osc_delta": round(osc_delta, 4),
                "price_delta": round(price_delta, 4),
                "strength": strength,
            }

    return None


def detect_divergences(candles, left=2, right=2, min_sep=5, max_sep=60,
                       max_age=12, min_price_atr=0.15,
                       stoch_settings=((9, 3, 5, "stoch_935"),
                                       (14, 3, 3, "stoch_1433")),
                       rsi_period=14, macd_settings=(12, 26, 9),
                       include_obv=False):
    """All recent divergence pairs for both directions.

    The result is sorted strongest first.  Callers choose the subset they want
    (regular/hidden, one oscillator, one direction) with ``best_divergence``.
    """
    if len(candles) < 50:
        return []
    atr_value = atr(candles)
    results = []

    for k, d, smooth, name in stoch_settings:
        k_line, _d_line = stoch_series(candles, k, d, smooth)
        for direction in ("LONG", "SHORT"):
            for kind in ("regular", "hidden"):
                pair = _detect_pair(
                    candles, k_line, name, direction, kind, atr_value,
                    left, right, min_sep, max_sep, max_age, min_price_atr,
                    min_osc_delta=5.0, zone_low=25.0, zone_high=75.0)
                if pair:
                    results.append(pair)

    rsi = rsi_series(candles, rsi_period)
    for direction in ("LONG", "SHORT"):
        for kind in ("regular", "hidden"):
            pair = _detect_pair(
                candles, rsi, f"rsi_{rsi_period}", direction, kind, atr_value,
                left, right, min_sep, max_sep, max_age, min_price_atr,
                min_osc_delta=3.0, zone_low=35.0, zone_high=65.0)
            if pair:
                results.append(pair)

    _macd, _signal, histogram = macd_series(candles, *macd_settings)
    macd_delta = max(0.05 * atr_value, 0.02) if atr_value else 0.05
    for direction in ("LONG", "SHORT"):
        for kind in ("regular", "hidden"):
            pair = _detect_pair(
                candles, histogram, "macd_hist", direction, kind, atr_value,
                left, right, min_sep, max_sep, max_age, min_price_atr,
                min_osc_delta=macd_delta, zone_low=None, zone_high=None)
            if pair:
                results.append(pair)

    if include_obv:
        obv = obv_series(candles)
        if obv:
            # OBV's scale depends on the feed's volume, so use a percentage of
            # the recent range rather than a fixed point threshold.
            recent = [v for v in obv[-60:] if v is not None]
            span = (max(recent) - min(recent)) if recent else 0.0
            min_delta = max(span * 0.02, 1.0)
            for direction in ("LONG", "SHORT"):
                for kind in ("regular", "hidden"):
                    pair = _detect_pair(
                        candles, obv, "obv", direction, kind, atr_value,
                        left, right, min_sep, max_sep, max_age,
                        min_price_atr, min_osc_delta=min_delta,
                        zone_low=None, zone_high=None)
                    if pair:
                        results.append(pair)

    results.sort(key=lambda item: (-item["strength"], item["age"]))
    return results


def best_divergence(divergences, direction, types=None, oscillators=None):
    """Strongest recent divergence for a direction, optionally filtered."""
    types = tuple(types or ())
    oscillators = tuple(oscillators or ())
    candidates = []
    for item in divergences or []:
        if item.get("direction") != direction:
            continue
        if types and not any(item["type"].startswith(t) for t in types):
            continue
        if oscillators and not any(item["oscillator"] == o or
                                   item["oscillator"].startswith(o)
                                   for o in oscillators):
            continue
        candidates.append(item)
    if not candidates:
        return None
    return max(candidates, key=lambda item: (item["strength"], -item["age"]))


def describe(item):
    if not item:
        return "none"
    return (f"{item['type']} {item['oscillator']} "
            f"(age {item['age']}b, strength {item['strength']:.1f})")
