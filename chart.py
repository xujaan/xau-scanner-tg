"""
Render a signal chart as PNG.

Price panel : 5M candles, order block, FVG, liquidity levels,
              Entry / Stop Loss / Take Profit lines
RSI panel   : RSI(14) with 70 / 30 bands

Rendering only - no trading logic lives here.
"""
import os
import tempfile

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib import gridspec
from matplotlib.lines import Line2D
from matplotlib.patches import Patch, Rectangle

BG, FG, GRID = "#0e1117", "#d7dce3", "#2a2f3a"
GREEN, RED, ENTRY_C = "#26a69a", "#ef5350", "#4f9cf9"
OB_C, FVG_C, LIQ_C, RSI_C = "#f0b90b", "#9b6bf2", "#ff9f43", "#c792ea"
MUTED = "#8b93a1"
WATERMARK = "https://t.me/xujaan"


def levels(sig):
    """Entry / SL / TP prices from the signal dict."""
    entry = float(sig["price"])
    sl_d, tp_d = float(sig["sl_dollars"]), float(sig["tp_dollars"])
    if sig["signal"] == "LONG":
        return entry, entry - sl_d, entry + tp_d
    return entry, entry + sl_d, entry - tp_d


def rsi_series(closes, period=14):
    """Wilder RSI; leading values are None until enough data."""
    out = [None] * len(closes)
    if len(closes) <= period:
        return out
    gain = loss = 0.0
    for i in range(1, period + 1):
        d = closes[i] - closes[i - 1]
        gain += max(d, 0.0)
        loss += max(-d, 0.0)
    gain /= period
    loss /= period
    out[period] = 100.0 if loss == 0 else 100 - 100 / (1 + gain / loss)
    for i in range(period + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        gain = (gain * (period - 1) + max(d, 0.0)) / period
        loss = (loss * (period - 1) + max(-d, 0.0)) / period
        out[i] = 100.0 if loss == 0 else 100 - 100 / (1 + gain / loss)
    return out


def fvg_zone(data):
    """Most recent 3-candle imbalance: (low_edge, high_edge, label)."""
    for i in range(len(data) - 1, 1, -1):
        if data[i]["low"] > data[i - 2]["high"]:
            return data[i - 2]["high"], data[i]["low"], "FVG bullish"
        if data[i]["high"] < data[i - 2]["low"]:
            return data[i]["high"], data[i - 2]["low"], "FVG bearish"
    return None


def _tag(ax, x, y, txt, col, size=8.5):
    """Label with an opaque pad so the line never cuts through the text."""
    ax.text(x, y, txt, color=col, fontsize=size, fontweight="bold",
            va="center", ha="left", zorder=9, clip_on=False,
            bbox=dict(boxstyle="round,pad=0.28", facecolor=BG,
                      edgecolor=col, linewidth=0.7, alpha=0.95))


def build_signal_chart(sig, candles, bars=60, path=None):
    if not candles:
        return None
    data = candles[-bars:]
    entry, sl, tp = levels(sig)
    is_long = sig["signal"] == "LONG"
    n = len(data)

    rsi_all = rsi_series([c["close"] for c in candles], 14)
    rsi = rsi_all[-n:]
    rsi_now = next((v for v in reversed(rsi) if v is not None), None)

    fig = plt.figure(figsize=(11, 7.4), dpi=140)
    fig.patch.set_facecolor(BG)
    gs = gridspec.GridSpec(2, 1, height_ratios=[4.4, 1.15], hspace=0.10)
    ax = fig.add_subplot(gs[0])
    axr = fig.add_subplot(gs[1], sharex=ax)
    for a in (ax, axr):
        a.set_facecolor(BG)

    # ---------- zones (drawn first, behind everything) ----------
    ob_h, ob_l = sig.get("ob_high"), sig.get("ob_low")
    if ob_h and ob_l:
        ax.axhspan(float(ob_l), float(ob_h), color=OB_C, alpha=0.11, zorder=1)

    fvg = fvg_zone(data)
    if fvg:
        lo_e, hi_e, fvg_lbl = fvg
        ax.axhspan(lo_e, hi_e, color=FVG_C, alpha=0.10, zorder=1)

    # ---------- liquidity levels ----------
    if sig.get("eq_highs"):
        lvl = max(c["high"] for c in data)
        ax.axhline(lvl, color=LIQ_C, linewidth=0.9, linestyle=(0, (1, 2)),
                   alpha=0.9, zorder=2)
    if sig.get("eq_lows"):
        lvl = min(c["low"] for c in data)
        ax.axhline(lvl, color=LIQ_C, linewidth=0.9, linestyle=(0, (1, 2)),
                   alpha=0.9, zorder=2)

    # ---------- legend for indicator zones ----------
    handles, labels = [], []
    if ob_h and ob_l:
        handles.append(Patch(facecolor=OB_C, alpha=0.45)); labels.append("order block")
    if fvg:
        handles.append(Patch(facecolor=FVG_C, alpha=0.45)); labels.append(fvg[2])
    if sig.get("eq_highs"):
        handles.append(Line2D([0], [0], color=LIQ_C, lw=1.0, ls=(0, (1, 2))))
        labels.append("equal highs (liquidity)")
    if sig.get("eq_lows"):
        handles.append(Line2D([0], [0], color=LIQ_C, lw=1.0, ls=(0, (1, 2))))
        labels.append("equal lows (liquidity)")
    if handles:
        leg = ax.legend(handles, labels, loc="upper left", fontsize=7, frameon=True,
                        facecolor=BG, edgecolor=GRID, framealpha=0.8, borderpad=0.55,
                        handlelength=1.7, labelcolor=FG, handletextpad=0.6)
        leg.set_zorder(11)

    # ---------- candles ----------
    for i, c in enumerate(data):
        o, h, l, cl = c["open"], c["high"], c["low"], c["close"]
        col = GREEN if cl >= o else RED
        ax.vlines(i, l, h, color=col, linewidth=0.9, zorder=3)
        lo, hi = min(o, cl), max(o, cl)
        if hi - lo < 0.02:
            hi = lo + 0.02
        ax.add_patch(Rectangle((i - 0.31, lo), 0.62, hi - lo,
                               facecolor=col, edgecolor=col, linewidth=0.5, zorder=4))

    # ---------- risk / reward bands + trade lines ----------
    ax.axhspan(sl, entry, color=RED, alpha=0.09, zorder=1)
    ax.axhspan(entry, tp, color=GREEN, alpha=0.09, zorder=1)
    ax.axhline(entry, color=ENTRY_C, linewidth=1.2, zorder=5)
    ax.axhline(sl, color=RED, linewidth=1.0, linestyle="--", zorder=5)
    ax.axhline(tp, color=GREEN, linewidth=1.0, linestyle="--", zorder=5)

    right = n + 1
    _tag(ax, right, entry, f"ENTRY {entry:.2f}", ENTRY_C)
    _tag(ax, right, sl, f"SL {sl:.2f}  (-{float(sig['sl_dollars']):.2f})", RED)
    _tag(ax, right, tp, f"TP {tp:.2f}  (+{float(sig['tp_dollars']):.2f})", GREEN)

    # ---------- axes cosmetics ----------
    step = max(1, n // 8)
    ticks = list(range(0, n, step))
    ax.tick_params(axis="both", colors=FG, labelsize=8, length=0)
    ax.set_xticks(ticks)
    ax.tick_params(axis="x", labelbottom=False)
    ax.grid(True, axis="y", color=GRID, alpha=0.5, linewidth=0.5, linestyle=":")

    vals = [c["low"] for c in data] + [c["high"] for c in data] + [sl, tp, entry]
    lo, hi = min(vals), max(vals)
    pad = (hi - lo) * 0.07
    ax.set_ylim(lo - pad, hi + pad)
    ax.set_xlim(-1, right + 10)
    for s in ("top", "right", "left", "bottom"):
        ax.spines[s].set_visible(False)

    dir_txt = "LONG (BUY)" if is_long else "SHORT (SELL)"
    ax.set_title(f"XAUUSD  5M  -  {dir_txt}   |   confidence {sig.get('confidence', '?')}%",
                 color=GREEN if is_long else RED, fontsize=14, fontweight="bold",
                 loc="left", pad=12)

    # ---------- RSI panel ----------
    xs = list(range(n))
    plotted = [(x, v) for x, v in zip(xs, rsi) if v is not None]
    if plotted:
        px, py = zip(*plotted)
        axr.plot(px, py, color=RSI_C, linewidth=1.1, zorder=3)
        axr.fill_between(px, py, 70, where=[v > 70 for v in py],
                         color=RED, alpha=0.22, interpolate=True)
        axr.fill_between(px, py, 30, where=[v < 30 for v in py],
                         color=GREEN, alpha=0.22, interpolate=True)
        if rsi_now is not None:
            _tag(axr, right, rsi_now, f"RSI {rsi_now:.1f}", RSI_C, size=8)
    axr.axhline(70, color=RED, linewidth=0.8, linestyle=":", alpha=0.8)
    axr.axhline(30, color=GREEN, linewidth=0.8, linestyle=":", alpha=0.8)
    axr.axhline(50, color=GRID, linewidth=0.6, linestyle=":", alpha=0.9)
    axr.set_ylim(10, 90)
    axr.set_yticks([30, 50, 70])
    axr.tick_params(axis="y", colors=MUTED, labelsize=7, length=0)
    axr.tick_params(axis="x", length=0)
    axr.set_xticks(ticks)
    axr.set_xticklabels([data[i]["time"][11:16] for i in ticks],
                        fontsize=8, color=MUTED)
    axr.set_xlim(-1, right + 10)
    axr.grid(True, axis="y", color=GRID, alpha=0.35, linewidth=0.5, linestyle=":")
    for s in ("top", "right", "left", "bottom"):
        axr.spines[s].set_visible(False)
    axr.set_ylabel("RSI 14", color=MUTED, fontsize=8)

    # ---------- footer ----------
    fig.text(0.01, 0.012, f"{sig.get('tf_bias', '')}  |  {data[-1]['time']}",
             color=MUTED, fontsize=8, ha="left")
    fig.text(0.5, 0.012, WATERMARK, color=ENTRY_C, fontsize=8.5, ha="center",
             fontweight="bold")
    fig.text(0.995, 0.012, "PAPER SIGNAL - NOT EXECUTED", color=MUTED,
             fontsize=8, ha="right")

    fig.subplots_adjust(left=0.075, right=0.895, top=0.925, bottom=0.085, hspace=0.10)
    if path is None:
        fd, path = tempfile.mkstemp(prefix="xau_signal_", suffix=".png")
        os.close(fd)
    fig.savefig(path, facecolor=BG, bbox_inches="tight")
    plt.close(fig)
    return path
