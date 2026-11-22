# Divergence Strategy — Research, Implementation, and Backtest

Dokumen ini merangkum **seluruh pekerjaan divergence** di repo ini: definisi
pattern, aturan deteksi, kode, hasil backtest, config live, dan catatan
deployment. Anggap ini sebagai sumber utama untuk semua hal divergence.

Terakhir diperbarui: **23 November 2026**

---

## Daftar isi

1. [TL;DR](#1-tldr)
2. [Pattern yang dipakai](#2-pattern-yang-dipakai)
3. [Aturan deteksi](#3-aturan-deteksi)
4. [Oscillator dan setting](#4-oscillator-dan-setting)
5. [Peta kode](#5-peta-kode)
6. [Shadow variants dan policy](#6-shadow-variants-dan-policy)
7. [Backtest: data dan metode](#7-backtest-data-dan-metode)
8. [Hasil backtest](#8-hasil-backtest)
9. [Split-half, cost stress, portfolio mode](#9-split-half-cost-stress-portfolio-mode)
10. [Kesimpulan strategi](#10-kesimpulan-strategi)
11. [Config live](#11-config-live)
12. [Akun cent dan sizing](#12-akun-cent-dan-sizing)
13. [Deployment dan monitoring](#13-deployment-dan-monitoring)
14. [Referensi riset](#14-referensi-riset)
15. [Caveat dan next steps](#15-caveat-dan-next-steps)

---

## 1. TL;DR

- **Divergence murni bukan edge.** Semua filter divergence standalone negatif
  di replay 1 tahun XAUUSD MT5.
- Yang bekerja adalah **hidden divergence sebagai filter konteks**, bukan entry
  tunggal.
- Kombinasi terbaik yang stabil:

  ```text
  SMC signal
    + entry di protective half order block (OB edge)
    + 15M EMA(20/50) trend alignment
    + hidden divergence searah
    + ATR(14) >= 4
    + ADX(14) >= 20
    + SL 1.5×ATR, TP 2R
  ```

- Hasil signal-by-signal 1 tahun: **+73.67R**, E[R] **+0.163**, max DD
  **12.26R**, 453 trades.
- Split-half: **+29.27R** dan **+42.36R** — positif di kedua paruh.
- Cost stress $0.50/oz: **+65.47R**, E[R] +0.145, DD 13.21.
- Portfolio mode (max 1 posisi, 0.5% risk, daily 3R): **+50.53R**, E[R]
  +0.126, DD 16.02, 400 trades.
- **Jangan** pakai Stoch cross, OBV divergence, 15M confirmation, freshness,
  atau strength sebagai filter tambahan; semuanya tidak memperbaiki base.
- Untuk akun cent Headway: `CAPITAL=10000` (USC), `CENT_ACCOUNT=1`,
  `RISK_PERCENT=0.005`; tampilan modal $100 dan risk $0.50/trade.

---

## 2. Pattern yang dipakai

### 2.1 Regular divergence (reversal warning)

| Pattern | Price | Oscillator | Arah |
|---|---|---|---|
| Regular bullish | Lower low | Higher low | LONG |
| Regular bearish | Higher high | Lower high | SHORT |

Regular divergence adalah **peringatan reversal**, bukan entry. Di trend kuat
pattern ini bisa muncul berkali-kali sebelum benar-benar reversal.

### 2.2 Hidden divergence (continuation)

| Pattern | Price | Oscillator | Arah |
|---|---|---|---|
| Hidden bullish | Higher low | Lower low | LONG |
| Hidden bearish | Lower high | Higher high | SHORT |

Hidden divergence adalah **continuation**: pullback dalam trend, lalu trend
lanjut. Ini yang paling cocok dengan engine SMC yang sudah punya 15M bias.

### 2.3 Pattern yang belum diimplementasikan

- **Triple divergence** (3 pivot berurutan) — sample terlalu kecil.
- **Multi-timeframe divergence 1H/4H** — 15M confirmation sudah diuji dan tidak
  memperbaiki hasil.
- **Volume profile divergence** — butuh data volume profile, bukan tick volume.
- **Harmonic patterns** (Gartley/Bat/Butterfly) — berbeda dari divergence
  indikator dan belum masuk scope.

---

## 3. Aturan deteksi

Implementasi ada di `divergence.py`.

### 3.1 Pivot

- Default `left=2`, `right=2`.
- Pivot low: low lebih rendah/equal dari 2 bar kiri dan lebih rendah dari 2 bar
  kanan.
- Pivot high: kebalikannya.
- Pivot **hanya valid setelah bar kanan close**. Tidak ada pivot yang memakai
  bar masa depan.

### 3.2 Pasangan pivot

- Jarak antar pivot: **5–60 bar**.
- Pivot terbaru harus berumur **≤ 12 bar** (`max_age`).
- Untuk setiap oscillator/direction/kind, detector mencari pasangan terbaru
  yang valid, bukan hanya dua pivot terakhir.
- Minimal price delta: **0.15 × ATR(14)**.
- Minimal oscillator delta:
  - Stochastic: **5 poin**
  - RSI: **3 poin**
  - MACD histogram: **0.05 × ATR**
  - OBV: **2% dari range OBV 60 bar**
- Zone filter untuk regular divergence:
  - Stochastic: bullish jika `min(K) <= 25`, bearish jika `max(K) >= 75`
  - RSI: bullish jika `min(RSI) <= 35`, bearish jika `max(RSI) >= 65`
  - MACD: tanpa zone
- Hidden divergence tidak memakai zone; valid selama trend context searah.

### 3.3 Output

Setiap divergence berbentuk dict:

```python
{
    "type": "regular_bullish" | "regular_bearish" |
            "hidden_bullish" | "hidden_bearish",
    "direction": "LONG" | "SHORT",
    "kind": "regular" | "hidden",
    "oscillator": "stoch_935" | "stoch_1433" | "rsi_14" |
                  "macd_hist" | "obv",
    "prev_idx": 54,
    "curr_idx": 97,
    "age": 2,
    "separation": 43,
    "price_prev": 4039.66,
    "price_curr": 4046.77,
    "osc_prev": 0.4377,
    "osc_curr": -0.269,
    "osc_delta": 0.7068,
    "price_delta": 7.11,
    "strength": 3.77,
}
```

`strength` adalah skor kasar 1–4 dari besar oscillator delta + price delta.
Dipakai untuk ranking, bukan sebagai satu-satunya filter.

---

## 4. Oscillator dan setting

### 4.1 Stochastic

Dua setting dihitung:

| Nama | %K | %D | Smoothing | Catatan |
|---|---:|---:|---:|---|
| `stoch_935` | 9 | 3 | 5 | default, lebih halus |
| `stoch_1433` | 14 | 3 | 3 | classic, lebih lambat |

Rumus:

```text
%K_raw = 100 * (close - LL(k)) / (HH(k) - LL(k))
%K     = SMA(%K_raw, smoothing)
%D     = SMA(%K, d)
```

Divergence memakai **smoothed %K**. `%D` tersedia untuk konfirmasi manual, tapi
**Stoch cross bukan filter live** karena hasil backtest negatif.

### 4.2 RSI

- Wilder RSI(14) untuk series divergence.
- Bot juga punya `calculate_rsi()` single-value (simple average) untuk display
  dan scoring lama; divergence memakai series Wilder yang terpisah.

### 4.3 MACD

- EMA 12/26, signal EMA 9.
- Divergence memakai **histogram** (`macd - signal`).
- Threshold delta dinormalisasi dengan ATR.

### 4.4 OBV

- Memakai `tick_volume` MT5.
- OBV divergence dihitung terpisah dari daftar divergence utama.
- **Tidak dipakai live** karena hasil backtest buruk (DD 37R).

### 4.5 ADX

- Wilder ADX(14) pada 5M.
- Bukan oscillator divergence; dipakai sebagai **trend-strength filter**.
- `MIN_ADX=20` adalah tambahan yang paling konsisten di split-half.

### 4.6 1H EMA

- Backtest-only saat ini.
- 1H EMA(20/50) dibentuk dari agregasi 15M.
- Hasil 1 tahun: E[R] 0.158, DD 12.64; split-half +32.41R/+42.90R.
- Live belum di-wire karena butuh 1H data/300 bar 15M. Alternatif yang lebih
  sederhana adalah ADX20.

---

## 5. Peta kode

### 5.1 `divergence.py`

| Fungsi | Kegunaan |
|---|---|
| `sma_series`, `ema_series` | Helper indicator |
| `rsi_series` | Wilder RSI series |
| `stoch_series` | Slow stochastic %K/%D |
| `macd_series` | MACD line, signal, histogram |
| `obv_series` | OBV dari tick/real volume |
| `atr` | ATR(14) |
| `adx_series`, `adx` | Wilder ADX |
| `pivot_lows`, `pivot_highs` | Confirmed pivots |
| `_detect_pair` | Deteksi satu pasangan divergence |
| `detect_divergences` | Semua oscillator/direction/kind |
| `best_divergence` | Filter direction/type/oscillator/age/strength |
| `describe` | String untuk log/Telegram |

### 5.2 `bot.py`

`generate_signal()` menambahkan field ke `sig`:

```python
"divergence": best_div,          # best 5M non-OBV untuk arah signal
"divergences": divergences,      # semua 5M non-OBV
"divergences_obv": divergences_obv,
"divergences_15m": divergences_15m,
"divergences_15m_obv": divergences_15m_obv,
"stoch_k": stoch_k_value,
"stoch_d": stoch_d_value,
"stoch_cross_up": stoch_cross_up,
"stoch_cross_down": stoch_cross_down,
"adx": adx_value,
```

Helper `has_divergence(sig, types, oscillators, timeframe, max_age,
min_strength)` dipakai oleh shadow logger, risk gate, dan backtest.

### 5.3 `backtest.py`

Filter yang tersedia:

```text
div_any, div_regular, div_hidden, div_stoch, div_stoch_1433,
div_rsi, div_macd, div_15m, div_fresh, div_strong, div_obv,
stoch_cross, adx_20, adx_25, adx_lt20, htf_ema_1h
```

Mode `--portfolio` menambahkan gate live:

- max 1 posisi terbuka
- daily loss brake 3R
- min-lot risk cap (capital × risk% × 2)
- `rej` column untuk jumlah sinyal yang ditolak risk cap

### 5.4 `learning.py`

Policy mapping untuk:

- `div_*` variants
- `ob_edge_htf_div`
- `ob_edge_htf_div_15m`
- `ob_edge_htf_div_adx20/adx25`
- `ob_edge_htf_div_stoch/obv/fresh/strong`
- `ob_edge`, `ob_edge_htf`

Policy gate di `bot.py` menegakkan `require_divergence`,
`divergence_types`, `divergence_oscillators`, `divergence_timeframe`,
`divergence_max_age`, `divergence_min_strength`, `min_adx`, `max_adx`,
`require_stoch_cross`, `require_ob_edge`, `require_htf_ema`.

### 5.5 `execution.py`

Eksekusi demo/live melalui MT5 bridge:

- `order_check` sebelum order
- market order + SL/TP
- magic + comment
- cek posisi yang sudah ada
- volume minimum 0.01
- return fill price + ticket

Diaktifkan hanya dengan `EXECUTION_ENABLED=1` dan `PAPER_MODE=0`.

---

## 6. Shadow variants dan policy

### 6.1 Divergence variants

| Variant | Rule |
|---|---|
| `div_any` | Ada divergence searah |
| `div_regular` | Regular divergence |
| `div_hidden` | Hidden divergence |
| `div_stoch` | Divergence dari Stoch 9,3,5 |
| `div_stoch_1433` | Divergence dari Stoch 14,3,3 |
| `div_rsi` | Divergence RSI |
| `div_macd` | Divergence MACD histogram |
| `div_obv` | Divergence OBV |
| `div_15m` | Divergence di 15M |
| `div_fresh` | Age ≤ 5 bar |
| `div_strong` | Strength ≥ 3 |
| `stoch_cross` | %K cross %D searah signal |
| `adx_20`, `adx_25`, `adx_lt20` | ADX filter |

### 6.2 Combo variants

| Variant | Rule |
|---|---|
| `ob_edge` | Entry di protective half OB |
| `ob_edge_htf` | OB edge + 15M EMA |
| `ob_edge_htf_div` | + hidden divergence |
| `ob_edge_htf_div_15m` | + 15M divergence confirmation |
| `ob_edge_htf_div_adx20` | + ADX ≥ 20 |
| `ob_edge_htf_div_adx25` | + ADX ≥ 25 |
| `ob_edge_htf_div_stoch` | + Stoch cross |
| `ob_edge_htf_div_obv` | + OBV divergence |
| `ob_edge_htf_div_fresh` | + freshness ≤ 5 |
| `ob_edge_htf_div_strong` | + strength ≥ 3 |

`/shadow` membandingkan semua varian dengan closed-bar evaluator dan cost
`SHADOW_SPREAD` (default 0.35/oz setelah update).

---

## 7. Backtest: data dan metode

- **Instrument:** XAUUSD spot dari MT5 bridge (`XAUUSD`, bukan `XAUUSD.m`)
- **Periode:** 21 Nov 2025 – 21 Nov 2026
- **Bar:** 70.634 × 5M, 23.625 × 15M
- **Timezone:** broker server time GMT+2/GMT+3 → UTC (`Europe/Kyiv`)
- **Biaya:** spread per bar MT5 (points × $0.01); bukan flat $0.20
- **Engine:** `bot.generate_signal()` yang sama dengan live
- **Exit:** SL 1.5×ATR, TP 2R, max 288 bar (24 jam)
- **No lookahead:** pivot menunggu bar kanan close; sinyal memakai closed bars
- **Signal cache:** `generate_signal` dihitung sekali per bar, lalu dipakai
  semua varian (full matrix tidak mengulang engine 40×)

Contoh command:

```bash
# full-year, per-bar spread
python backtest.py --data mt5 --start 2025-11-21 --end 2026-11-21 \
  --bar-spread

# divergence only
python backtest.py --data mt5 --bar-spread --only "div"

# one combo
python backtest.py --data mt5 --bar-spread \
  --only "OB+EMA+div+ATR4+ADX20"

# live-like portfolio gates
python backtest.py --data mt5 --bar-spread --portfolio \
  --capital 10000 --risk-percent 0.005 \
  --only "OB+EMA+div+ATR4+ADX20"
```

---

## 8. Hasil backtest

### 8.1 Baseline dan divergence murni

| Variant | Trades | WR | E[R] | Total R | Max DD |
|---|---:|---:|---:|---:|---:|
| Baseline engine | 2.678 | 35% | +0.007 | +19.32 | 69.93 |
| Divergence any | 2.414 | 34% | −0.020 | −47.26 | 96.94 |
| Divergence hidden | 2.176 | 34% | −0.021 | −44.72 | 96.91 |
| Divergence Stoch 9,3,5 | 2.194 | 35% | −0.016 | −34.40 | 81.38 |
| Divergence Stoch 14,3,3 | 2.147 | 34% | −0.020 | −43.95 | 84.48 |
| Divergence RSI | 1.785 | 34% | −0.031 | −54.55 | 118.30 |
| Divergence MACD | 2.242 | 35% | −0.012 | −26.21 | 81.29 |

**Semua standalone negatif.** Ini konsisten dengan riset publik: RSI
divergence murni di 15M umumnya rugi setelah biaya.

### 8.2 Combo yang bekerja

| Variant | Trades | WR | E[R] | Total R | Max DD |
|---|---:|---:|---:|---:|---:|
| OB edge | 1.538 | 37% | +0.059 | +90.57 | 30.93 |
| OB edge + 15M EMA | 1.172 | 38% | +0.092 | +108.32 | 25.97 |
| **OB edge + 15M EMA + hidden div** | **1.006** | **39%** | **+0.118** | **+118.78** | **18.88** |
| **+ ATR4** | **675** | **39%** | **+0.132** | **+88.87** | **18.21** |
| **+ ATR4 + ADX20** | **453** | **40%** | **+0.163** | **+73.67** | **12.26** |
| + ATR4 + ADX25 | 299 | 43% | +0.270 | +80.60 | 12.50 |
| + ATR4 + 1H EMA | 502 | 40% | +0.158 | +79.51 | 12.64 |
| Short + ATR2 + OB | 1.023 | 40% | +0.150 | +153.38 | 32.18 |
| Short + 15M EMA + ATR2 + OB | 805 | 41% | +0.185 | +148.77 | 22.55 |

Short-only sangat kuat di sample ini, tapi itu **regime bet**; bukan default
yang disarankan untuk akun kecil.

### 8.3 Round 2: filter konteks tambahan

| Added filter | Trades | WR | E[R] | Total R | Max DD |
|---|---:|---:|---:|---:|---:|
| Base ATR4 | 675 | 39% | +0.132 | +88.87 | 18.21 |
| ADX ≥ 20 | 453 | 40% | +0.163 | +73.67 | 12.26 |
| ADX ≥ 25 | 299 | 43% | +0.270 | +80.60 | 12.50 |
| 1H EMA | 502 | 40% | +0.158 | +79.51 | 12.64 |
| 15M divergence | 592 | 39% | +0.124 | +73.41 | 19.12 |
| Freshness ≤ 5 | 423 | 38% | +0.106 | +44.71 | 16.53 |
| Strength ≥ 3 | 671 | 39% | +0.130 | +87.01 | 18.21 |
| OBV divergence | 444 | 38% | +0.099 | +43.90 | 37.34 |
| Stoch cross | 101 | 32% | −0.084 | −8.52 | 12.77 |

Yang **tidak** dipakai: Stoch cross, OBV, 15M confirmation, freshness,
strength. Yang dipakai: **ADX20**.

---

## 9. Split-half, cost stress, portfolio mode

### 9.1 Split-half

| Variant | Nov–Mei | Mei–Nov |
|---|---:|---:|
| Base ATR4 | +37.71R, E[R] +0.106, DD 18.21 | +46.04R, E[R] +0.142, DD 14.86 |
| **ADX20** | **+29.27R, E[R] +0.122, DD 11.95** | **+42.36R, E[R] +0.196, DD 11.73** |
| ADX25 | +20.23R, E[R] +0.126, DD 12.50 | +59.33R, E[R] +0.427, DD 7.40 |
| 1H EMA | +32.41R, E[R] +0.131, DD 11.24 | +42.90R, E[R] +0.164, DD 12.64 |
| ATR2 + OB + hidden div | +0.69R, E[R] +0.001, DD 31.29 | +65.76R, E[R] +0.097, DD 28.65 |

- **ADX20** paling konsisten: dua paruh positif dan DD ~12R.
- ADX25 E[R] paling tinggi, tapi edge-nya berat di paruh kedua.
- 1H EMA juga konsisten; lebih banyak trade, tapi live belum di-wire.
- ATR2 tidak robust.

### 9.2 Cost stress $0.50/oz

| Config | Trades | WR | E[R] | Total R | Max DD |
|---|---:|---:|---:|---:|---:|
| Non-ATR4 combo | 1.006 | 39% | +0.095 | +95.60 | 22.69 |
| ATR4 combo | 675 | 39% | +0.113 | +76.52 | 18.22 |
| **ATR4 + ADX20** | **453** | **40%** | **+0.145** | **+65.47** | **13.21** |

Biaya $0.50/oz lebih buruk dari spread broker normal; ADX20 tetap positif.

### 9.3 Portfolio mode (live-like)

Max 1 posisi, daily loss 3R, min-lot risk cap, capital $10.000, risk 0.5%:

| Config | Trades | WR | E[R] | Total R | Max DD | Rejected |
|---|---:|---:|---:|---:|---:|---:|
| Base ATR4 | 561 | 38% | +0.090 | +50.75 | 19.51 | 0 |
| **ATR4 + ADX20** | **400** | **39%** | **+0.126** | **+50.53** | **16.02** | **0** |

Portfolio gates memotong trade dan total R (karena tidak boleh overlap), tapi
ADX20 tetap lebih baik per trade dan DD lebih rendah.

---

## 10. Kesimpulan strategi

1. **Jangan trade divergence murni.**
2. **Gunakan hidden divergence searah trend** sebagai filter, bukan trigger.
3. **Konteks wajib:** order block edge, 15M EMA, ATR, dan ADX.
4. **Regular divergence** hanya berguna sebagai warning, bukan entry.
5. **Stoch 9,3,5** valid sebagai sumber divergence, tapi Stoch cross bukan
   filter yang baik.
6. **OBV/volume divergence** tidak membantu di XAU 5M sample ini.
7. **ADX20** adalah tambahan yang paling stabil; ADX25 untuk buku yang lebih
   selektif.
8. **1H EMA** menjanjikan, tapi masih backtest-only.
9. **Risk 0.5–1% per trade**; jangan 2%.
10. **Shadow dulu** minimal 100 closed trades sebelum menaikkan risiko.

---

## 11. Config live

Default kode sekarang:

```env
REQUIRE_OB_EDGE=1
REQUIRE_HTF_EMA=1
REQUIRE_DIVERGENCE=1
DIVERGENCE_FILTER_TYPES=hidden
DIVERGENCE_LIVE_MODE=shadow
ATR_STOP_MULT=1.5
MIN_ENTRY_ATR=4.0
MIN_ADX=20
```

Arti:

- `DIVERGENCE_LIVE_MODE=shadow`: sinyal mentah tetap dihitung dan dicatat,
  gate live diterapkan di `risk_gate()`.
- `REQUIRE_DIVERGENCE=1`: live hanya mengirim sinyal dengan hidden divergence
  searah.
- `DIVERGENCE_LIVE_MODE=filter` **tidak disarankan** kalau masih ingin baseline
  shadow, karena filter terjadi di dalam `generate_signal()` dan sinyal mentah
  tidak tercatat.

Untuk kembali ke baseline lama:

```env
REQUIRE_OB_EDGE=0
REQUIRE_HTF_EMA=0
REQUIRE_DIVERGENCE=0
MIN_ADX=0
```

---

## 12. Akun cent dan sizing

Akun demo Headway adalah **USC cent account**:

```text
balance 10.000,00 USC = $100,00
```

Yang benar:

```env
CENT_ACCOUNT=1
CAPITAL=10000
RISK_PERCENT=0.005
```

Jangan set `CAPITAL=100`; itu membuat gate min-lot menolak hampir semua sinyal.

Dengan setting di atas:

- Risk/trade = 50 USC = **$0.50**
- Stop ~$10 → sekitar **0.05 lot**
- `order_check` 0.05 lot → margin **8.86 USC**, retcode **0**
- Max 1 posisi, daily stop 3R

Untuk akun USD standar: `CENT_ACCOUNT=0`, `CAPITAL` dalam USD, gunakan
`RISK_PERCENT=0.005–0.01`.

---

## 13. Deployment dan monitoring

Status deployment saat dokumen ini dibuat:

| Item | Nilai |
|---|---|
| Deployment | `xau-scanner` / namespace `third-party` |
| Image | `xujaan/xau-scanner-tg:latest` |
| Digest | `sha256:bd6dafa4...` |
| Pod | `xau-scanner-697bdcdc4b-79svg` |
| Commits | `f691cc6`, `4a38829`, `bee7776` |
| Data | MT5 `XAUUSD` + TwelveData fallback |
| Execution | `PAPER_MODE=0`, `EXECUTION_ENABLED=1` |
| Cent | `CENT_ACCOUNT=1`, `CAPITAL=10000` |
| AutoTrading | true |
| Policy | none |

Monitor:

```bash
kubectl -n third-party logs -f deploy/xau-scanner
curl -s https://mt5-api.gnt.my.id/health
```

Stop execution:

```bash
kubectl -n third-party set env deploy/xau-scanner EXECUTION_ENABLED=0
# atau
kubectl -n third-party set env deploy/xau-scanner PAPER_MODE=1
```

Catatan:

- Market XAUUSD buka sekitar **23:00 UTC / 06:00 WIB**.
- Guard staleness mencegah entry dari bar Jumat saat market baru buka.
- Scan pertama yang valid biasanya setelah bar 15M pertama (sekitar 23:15 UTC).
- Kalau pod MT5 restart, AutoTrading bisa kembali off; fokus klik chart + Ctrl+E
  diperlukan. Bot akan mengirim `EXECUTION FAILED` dan tidak entry.

---

## 14. Referensi riset

Referensi ini dipakai sebagai **hipotesis**, bukan bukti:

- **BacktestingMax — RSI Divergence Strategy Backtest**: 24 test, 2014–2025;
  hanya 5 profit, semuanya 1H/4H; 15M rugi.
- **TradingSim — Divergence Trading**: multi-timeframe alignment.
- **StratBase — RSI Divergence Trading Guide**: regular vs hidden,
  confirmation.
- **LuxAlgo — Ultimate Guide to Stochastic Divergence**: ADX/context filters.
- **1trader — Stochastic 9/3/3**: execution-timeframe stochastic.
- **Headway FAQ**: investor password untuk view-only mobile.

Yang diambil dari riset:

- divergence murni lemah;
- hidden divergence lebih masuk akal sebagai continuation;
- trend/level/ADX context lebih penting daripada ganti oscillator;
- confirmation sederhana tidak otomatis memperbaiki edge.

---

## 15. Caveat dan next steps

### Caveat

- Backtest **in-sample**, satu broker, satu tahun, satu regime.
- Portfolio mode belum memodelkan news blackout, swap, komisi, atau slippage
  selain spread.
- Min-lot gate membuat strategi ini tidak cocok untuk akun $200.
- `order_check` bukan bukti fill; eksekusi live baru terbukti setelah market
  buka.
- ADX25 punya sample lebih kecil dan edge half2-heavy.
- 1H EMA masih backtest-only.
- AutoTrading MT5 bisa mati setelah restart pod.

### Next steps

1. Jalankan shadow/live minimal 100 closed trades.
2. Bandingkan `ob_edge_htf_div_adx20` vs `ob_edge_htf_div_adx25` di `/shadow`.
3. Kalau ADX20 forward E[R] > 0.10 dan DD < 20R, pertimbangkan naik risk ke
   0.75–1%.
4. Kalau forward E[R] negatif, revert gate ke baseline dan investigasi regime.
5. Pertimbangkan wire 1H EMA live setelah sample 15M/ADX terbukti.
6. Pertimbangkan triple divergence hanya jika sample cukup; jangan paksakan.

---

## Changelog

- **2026-11-23** — Dokumen dibuat; merangkum implementasi, backtest 1 tahun,
  Round 2, split-half, cost stress, portfolio mode, config cent account, dan
  deployment.
