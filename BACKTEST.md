# MT5 XAUUSD Backtest — 2025-11-21 to 2026-11-21

## Data and method

- **Instrument:** XAUUSD spot, MT5 bridge (`XAUUSD`, not the chart suffix `XAUUSD.m`)
- **Bars:** 70,634 × 5-minute, 23,625 × 15-minute
- **Timestamps:** broker server time converted to UTC (`Europe/Kyiv`, GMT+2/GMT+3)
- **Costs:** each entry bar's own MT5 spread, in points × $0.01; no commission or slippage beyond spread
- **Engine:** the live `bot.generate_signal()` and the same closed-bar rule; 1:2 R:R, 1.5×ATR(14) stop, 288-bar time stop
- **No lookahead:** pivots need their right-side bars closed; signals use only bars available at the time
- **Not modelled in the signal-by-signal tables:** portfolio guards (max one open position, heat cap, daily stop), the min-lot risk gate, news blackout, swap/commission, and the live 45-minute same-direction throttle (the backtest has its own cooldown). The final `--portfolio` run does apply max one open position, the daily 3R brake and the min-lot risk cap.
- **Account sizing:** with the default `CAPITAL=200` and 2% risk, the combo's ~$10 average stop is above the `2×` min-lot risk cap ($8), so the live bot would reject many of these signals. Set `CAPITAL` to the real balance, or use a larger account / smaller risk target.

Because the variant list was compared on the same year, these are **in-sample** results. Treat them as a ranking and a hypothesis, not a promise.

## What the public divergence research says

The reproducible public tests point in the same direction as this replay:

- A 24-test RSI-divergence study across FX, gold and indices found only 5 profitable tests, all on 1H or 4H; every 15-minute test lost after costs. Waiting for a simple confirmation did not fix it.
- Other studies find the useful split is **hidden divergence as continuation**, not regular divergence as a standalone reversal.
- The recurring filters are higher-timeframe alignment, trend strength (ADX), a support/resistance or order-block context, and a volume/OBV check. None of them is a guarantee; each has to be tested on the instrument and timeframe.

That is why this repository does not trade divergence as a standalone signal. It tests divergence as a filter on top of the existing structure/OB engine, and it keeps the variants in shadow.

Sources used for the hypotheses (not as proof):

- BacktestingMax, *RSI Divergence Strategy Backtest* — 24 tests, 2014–2025
- TradingSim, *Divergence Trading* — multi-timeframe alignment filter
- StratBase, *RSI Divergence Trading Guide* — regular vs hidden, confirmation
- LuxAlgo, *Ultimate Guide to Stochastic Divergence Trading* — ADX/context filters
- 1trader, *Stochastic 9/3/3 settings* — execution-timeframe stochastic

## One-year results (selected)

| Variant | Trades | WR | E[R] | Total R | Max DD |
|---|---:|---:|---:|---:|---:|
| Baseline | 2,678 | 35% | +0.007 | +19.32 | 69.93 |
| Divergence any | 2,414 | 34% | −0.020 | −47.26 | 96.94 |
| Divergence hidden | 2,176 | 34% | −0.021 | −44.72 | 96.91 |
| OB edge | 1,538 | 37% | +0.059 | +90.57 | 30.93 |
| OB edge + 15M EMA | 1,172 | 38% | +0.092 | +108.32 | 25.97 |
| **OB edge + 15M EMA + hidden divergence** | **1,006** | **39%** | **+0.118** | **+118.78** | **18.88** |
| **OB edge + 15M EMA + hidden divergence + ATR4** | **675** | **39%** | **+0.132** | **+88.87** | **18.21** |
| ATR2 + OB + hidden divergence | 1,289 | 36% | +0.052 | +67.50 | 31.29 |
| ATR2 + OB + 15M EMA + hidden divergence | 1,006 | 38% | +0.093 | +93.98 | 31.10 |
| Short + ATR2 + OB | 1,023 | 40% | +0.150 | +153.38 | 32.18 |
| Short + 15M EMA + ATR2 + OB | 805 | 41% | +0.185 | +148.77 | 22.55 |

**Divergence alone is not an edge.** Every standalone divergence filter lost money
over the year. It only helped when it was stacked on the OB-edge and 15M-EMA
filters, where it raised E[R] and cut drawdown. At 1:2 R:R the break-even win
rate is 33.3%; the combo's 39% is the actual edge, and it is thin enough that
costs matter.

## Round 2: context filters on top of the winning combo

The base row is `OB edge + 15M EMA + hidden divergence + ATR4`. Each row adds
one context filter to that base, using the same 1-year MT5 sample and per-bar
spread.

| Added filter | Trades | WR | E[R] | Total R | Max DD |
|---|---:|---:|---:|---:|---:|
| *(base)* | 675 | 39% | +0.132 | +88.87 | 18.21 |
| ADX >= 20 | 453 | 40% | +0.163 | +73.67 | 12.26 |
| **ADX >= 25** | **299** | **43%** | **+0.270** | **+80.60** | **12.50** |
| 1H EMA(20/50) instead of 15M EMA | 502 | 40% | +0.158 | +79.51 | 12.64 |
| 15M EMA + 1H EMA | 460 | 40% | +0.157 | +72.10 | 13.14 |
| 15M divergence confirmation | 592 | 39% | +0.124 | +73.41 | 19.12 |
| freshness (age <= 5) | 423 | 38% | +0.106 | +44.71 | 16.53 |
| divergence strength >= 3 | 671 | 39% | +0.130 | +87.01 | 18.21 |
| OBV divergence | 444 | 38% | +0.099 | +43.90 | 37.34 |
| Stochastic %K/%D cross | 101 | 32% | −0.084 | −8.52 | 12.77 |

**ADX >= 25 has the strongest headline E[R]:** it cuts the sample to 299 trades
but raises E[R] to +0.270 and cuts drawdown from 18.21R to 12.50R versus the
base. ADX >= 25 on the raw engine (without the OB/EMA/divergence stack) was
also strong: +140.35R, E[R] +0.117, DD 24.86 over 1,201 trades. The 1H EMA is
close and has more trades. Stochastic cross and OBV confirmation did not help;
15M confirmation and freshness were not better than the base. Those were useful
hypotheses to test, not features to ship.

## Split-half check

| Variant | First half (Nov–May) | Second half (May–Nov) |
|---|---:|---:|
| Baseline | −37.02R, E[R] −0.030, DD 69.93 | +56.21R, E[R] +0.039, DD 35.13 |
| Divergence any | −69.11R, E[R] −0.062, DD 96.94 | +21.72R, E[R] +0.017, DD 50.28 |
| OB edge + 15M EMA | +34.59R, E[R] +0.064, DD 21.31 | +68.60R, E[R] +0.107, DD 25.97 |
| **OB edge + 15M EMA + hidden divergence** | **+28.16R, E[R] +0.060, DD 18.88** | **+85.48R, E[R] +0.157, DD 16.80** |
| **OB edge + 15M EMA + hidden divergence + ATR4** | **+37.71R, E[R] +0.106, DD 18.21** | **+46.04R, E[R] +0.142, DD 14.86** |
| **+ ADX >= 20** | **+29.27R, E[R] +0.122, DD 11.95** | **+42.36R, E[R] +0.196, DD 11.73** |
| **+ ADX >= 25** | **+20.23R, E[R] +0.126, DD 12.50** | **+59.33R, E[R] +0.427, DD 7.40** |
| **+ 1H EMA instead of 15M EMA** | **+32.41R, E[R] +0.131, DD 11.24** | **+42.90R, E[R] +0.164, DD 12.64** |
| ATR2 + OB + hidden divergence | +0.69R, E[R] +0.001, DD 31.29 | +65.76R, E[R] +0.097, DD 28.65 |

The OB-edge + 15M-EMA + hidden-divergence combo is positive in **both** halves.
The ATR2 variant is not robust in the first half. Among the Round 2 filters:

- **ADX >= 20** is the most consistent addition: +29.27R then +42.36R, with
  E[R] +0.122/+0.196 and drawdown around 12R in both halves.
- **ADX >= 25** has the highest full-year E[R] (+0.270), but the edge is
  concentrated in the second half (+20.23R then +59.33R). Use it only if you
  prefer fewer, more selective trades.
- **1H EMA** is also consistent (+32.41R then +42.90R, E[R] +0.131/+0.164) and
  has more trades than ADX25. It is currently a backtest feature; the live bot
  would need 1H data (or 300 15M bars to build a completed 1H series).
- Stochastic cross, OBV divergence, 15M confirmation, freshness and strength
  did not improve the base combo enough to justify the extra rule.

## Cost stress

Flat cost of **$0.50/oz per trade** (above the broker's normal XAUUSD spread)
still leaves the combo positive:

| Variant | Trades | WR | E[R] | Total R | Max DD |
|---|---:|---:|---:|---:|---:|
| OB edge + 15M EMA + hidden divergence, $0.50/oz | 1,006 | 39% | +0.095 | +95.60 | 22.69 |
| OB edge + 15M EMA + hidden divergence + ATR4, $0.50/oz | 675 | 39% | +0.113 | +76.52 | 18.22 |
| **OB+EMA+div+ATR4+ADX20, $0.50/oz** | **453** | **40%** | **+0.145** | **+65.47** | **13.21** |

The normal per-bar spread run is the primary result; this is the pessimistic
cost check. The recommended ADX20 version still keeps E[R] +0.145 and a 13.21R
drawdown at the higher cost.

## Portfolio-mode final (live-like gates)

The signal-by-signal tables allow overlapping trades. The live bot does not: it
allows one open position, a daily 3R brake and a min-lot risk cap. Replaying the
same year with `--portfolio --capital 10000 --risk-percent 0.005` gives:

| Config | Trades | WR | E[R] | Total R | Max DD | Rejected |
|---|---:|---:|---:|---:|---:|---:|
| OB edge + 15M EMA + hidden divergence + ATR4 | 561 | 38% | +0.090 | +50.75 | 19.51 | 0 |
| **OB edge + 15M EMA + hidden divergence + ATR4 + ADX20** | **400** | **39%** | **+0.126** | **+50.53** | **16.02** | **0** |

The portfolio gates cut the number of trades and total R, as expected, but the
ADX20 version keeps the better per-trade edge and a lower drawdown. Both
configs made about +50R for the year at 0.5% risk; ADX20 did it with 161 fewer
trades and a 3.5R smaller drawdown.

## What this means for live

Recommended candidate, subject to forward shadow validation:

```env
REQUIRE_OB_EDGE=1
REQUIRE_HTF_EMA=1
REQUIRE_DIVERGENCE=1
DIVERGENCE_FILTER_TYPES=hidden
ATR_STOP_MULT=1.5
MIN_ENTRY_ATR=4.0
MIN_ADX=20
```

The recommended live gate is `REQUIRE_DIVERGENCE=1` with
`DIVERGENCE_LIVE_MODE=shadow` (the default). That keeps the raw signal in the
shadow logger for a fair baseline. `DIVERGENCE_LIVE_MODE=filter` is optional but
pre-filters inside `generate_signal`, so the raw baseline is no longer logged.
These are now the code defaults; to return to the old baseline, set
`REQUIRE_OB_EDGE=0`, `REQUIRE_HTF_EMA=0`, `REQUIRE_DIVERGENCE=0` and
`MIN_ADX=0`.

For a more selective book, use `MIN_ADX=25`; it has the highest full-year E[R]
but a smaller sample and a second-half-heavy edge. The `ob_edge_htf_div` and
`ob_edge_htf_div_adx20/25` shadow variants have matching promotion policies, so
the same combos can be evaluated by `/shadow` and promoted by `/learn` without
the env overrides. The ATR4 + ADX20 version is the preferred robust live
candidate; ATR4 without ADX has more total R but a higher drawdown. The
non-ATR4 and ATR4 versions both survive the $0.50/oz cost stress.

Risk: the recommended ADX20 combo's max drawdown was **12.26R** (ADX25 12.50R;
ATR4 without ADX 18.21R). At 2% risk per trade that is ~24.5%; at 0.5% it is
~6.1%. For this strategy, **0.5–1% per trade** is the sane range. The live
defaults should not be treated as a recommendation to risk 2% on an unproven
strategy.

The code defaults are now active, with the raw signal still recorded in shadow.
If you prefer the conservative path, set `REQUIRE_OB_EDGE=0`,
`REQUIRE_HTF_EMA=0`, `REQUIRE_DIVERGENCE=0` and `MIN_ADX=0` until the shadow
sample proves the combo forward. Either way, watch the first 100 closed
live/shadow trades and revert if forward E[R] or drawdown degrades. The
backtest is one year, one broker, one regime, and the combo was selected after
looking at that year.
