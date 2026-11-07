import json
import os
import requests
import sqlite3
import threading
import time
from datetime import datetime
import pytz
try:
    from chart import build_signal_chart
except Exception as _chart_err:
    build_signal_chart = None
    print(f"Chart disabled: {_chart_err}")


# ============================================
# CONFIGURATION
# ============================================
TELEGRAM_TOKEN     = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID")
TWELVEDATA_API_KEY = os.environ.get("TWELVEDATA_API_KEY")

# TwelveData is the data source. OANDA v20 support is opt-in and inert until
# DATA_PROVIDER=oanda is set, so no OANDA request is ever made by default.
DATA_PROVIDER     = os.environ.get("DATA_PROVIDER", "twelvedata").strip().lower()
OANDA_API_KEY     = os.environ.get("OANDA_API_KEY")
OANDA_ACCOUNT_ID  = os.environ.get("OANDA_ACCOUNT_ID")
OANDA_ENV         = os.environ.get("OANDA_ENV", "practice").strip().lower()
OANDA_INSTRUMENT  = os.environ.get("OANDA_INSTRUMENT", "XAU_USD")

# AI second opinion via OpenRouter. Off unless OPENROUTER_API_KEY is set, and it can
# never replace or alter the rule-based plan — it only annotates it.
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY")
OPENROUTER_MODEL   = os.environ.get("OPENROUTER_MODEL", "deepseek/deepseek-v4.1-flash-1017")
AI_ENABLED         = bool(OPENROUTER_API_KEY)
AI_TIMEOUT         = int(os.environ.get("AI_TIMEOUT", 20))
JOURNAL_FILE       = os.environ.get("JOURNAL_FILE", "signal_journal.json")
JOURNAL_MAX        = 200

# Durable storage: Turso/libSQL when credentials are set, otherwise a local SQLite
# file. DB problems are logged and swallowed — they must never stop the bot.
TURSO_DATABASE_URL = os.environ.get("TURSO_DATABASE_URL")
TURSO_AUTH_TOKEN   = os.environ.get("TURSO_AUTH_TOKEN")
DB_FILE            = os.environ.get("DB_FILE", "signals.db")

SYMBOL       = "XAU/USD"
CAPITAL      = 200
RISK_PERCENT = 0.05
RISK_AMOUNT  = CAPITAL * RISK_PERCENT   # $10

# Optional rolling quota: at most MAX_TRADES_PER_DAY signals in any SIGNAL_WINDOW_HOURS
# window. 0 (the default) means no cap at all — the same-direction cooldown below is
# then the only throttle. No midnight reset either way.
MAX_TRADES_PER_DAY      = int(os.environ.get("MAX_TRADES_PER_DAY", 0))
QUOTA_ENABLED           = MAX_TRADES_PER_DAY > 0
SIGNAL_WINDOW_HOURS     = float(os.environ.get("SIGNAL_WINDOW_HOURS", 24))
SIGNAL_WINDOW           = int(SIGNAL_WINDOW_HOURS * 3600)
SAME_DIRECTION_COOLDOWN = 2700   # seconds — ignore a repeat signal in the same direction
STATE_FILE              = os.environ.get("STATE_FILE", "signal_state.json")

MIN_CONFIDENCE     = 65
MIN_SL_DOLLARS     = 3.0
MAX_SL_DOLLARS     = 20.0
ATR_STOP_MULT      = 1.5    # SL distance in ATR(14) units
MIN_FVG_SIZE       = 1.50   # minimum FVG gap in dollars to count
EQUAL_LEVEL_TOL    = 0.30   # dollars — how close two highs/lows must be to count as "equal"
IST                = pytz.timezone('Asia/Kolkata')
NY                 = pytz.timezone('America/New_York')

PAPER_MODE = True

# Gold contract: 1.00 lot = 100oz → $1 move = $100/lot → $1 move = $1 per 0.01 lot
USD_PER_DOLLAR_MOVE_PER_001_LOT = 1.0

# ============================================
# STATE  (rolling window, persisted to disk)
# ============================================
# List of {"ts": epoch, "direction": "BUY"|"SELL"}, oldest first.
signals_sent = []

def load_state():
    """Read signal history from disk. Missing or corrupt file = empty history."""
    try:
        with open(STATE_FILE, "r") as fh:
            raw = json.load(fh)
        history = []
        for item in raw.get("signals", []):
            direction = {"BUY": "LONG", "SELL": "SHORT"}.get(str(item["direction"]),
                                                               str(item["direction"]))
            if direction in ("LONG", "SHORT"):
                history.append({"ts": float(item["ts"]), "direction": direction})
        history.sort(key=lambda s: s["ts"])
        return prune_state(history)
    except FileNotFoundError:
        return []
    except Exception as e:
        print(f"State load error ({STATE_FILE}): {e} — starting empty.")
        return []

def save_state(history):
    """Best-effort atomic write; a disk problem must not stop the bot."""
    tmp = f"{STATE_FILE}.tmp"
    try:
        with open(tmp, "w") as fh:
            json.dump({"signals": history}, fh)
        os.replace(tmp, STATE_FILE)
    except Exception as e:
        print(f"State save error ({STATE_FILE}): {e}")

def prune_state(history, now_ts=None):
    """Drop signals that have left the rolling window."""
    now_ts = time.time() if now_ts is None else now_ts
    return [s for s in history if now_ts - s["ts"] < SIGNAL_WINDOW]

def last_sent_ts(history, direction):
    """Newest timestamp for one direction. The whole window is scanned so an
    alternating signal cannot reset the same-direction cooldown."""
    for item in reversed(history):
        if item["direction"] == direction:
            return item["ts"]
    return None

def same_direction_wait(history, direction, now_ts=None):
    """Seconds left before another signal in `direction` is allowed."""
    now_ts = time.time() if now_ts is None else now_ts
    last = last_sent_ts(history, direction)
    if last is None:
        return 0
    return max(0, int(last + SAME_DIRECTION_COOLDOWN - now_ts))

def seed_state_from_journal(history, journal, now_ts=None):
    """Merge the durable journal into the throttle window.

    STATE_FILE lives on an emptyDir in k8s, so a restart forgets the cooldown
    while the journal survives in Turso. Without this the first scans after a
    deploy can fire a burst of duplicate signals.
    """
    now_ts = time.time() if now_ts is None else now_ts
    merged = list(history)
    for rec in journal:
        try:
            ts = float(rec["ts"])
        except (KeyError, TypeError, ValueError):
            continue
        direction = {"BUY": "LONG", "SELL": "SHORT"}.get(str(rec.get("direction")),
                                                           str(rec.get("direction")))
        if direction not in ("LONG", "SHORT") or now_ts - ts >= SIGNAL_WINDOW:
            continue
        # record_signal stamps the journal a few seconds after the state entry;
        # a one-minute tolerance dedupes the same alert without merging real ones.
        if any(s["direction"] == direction and abs(s["ts"] - ts) < 60 for s in merged):
            continue
        merged.append({"ts": ts, "direction": direction})
    merged.sort(key=lambda s: s["ts"])
    return prune_state(merged, now_ts)

def format_wait(history, now_ts=None):
    """Time until the oldest signal leaves the window and a slot frees up."""
    now_ts = time.time() if now_ts is None else now_ts
    if not history:
        return "now"
    wait = int(history[0]["ts"] + SIGNAL_WINDOW - now_ts)
    return "now" if wait <= 0 else f"{wait // 3600}h {(wait % 3600) // 60}m"

# ============================================
# DATABASE  (Turso/libSQL over HTTP, or local SQLite)
# ============================================
DB_SCHEMA = [
    """CREATE TABLE IF NOT EXISTS bars (
         symbol TEXT NOT NULL, timeframe TEXT NOT NULL, bar_time TEXT NOT NULL,
         open REAL, high REAL, low REAL, close REAL,
         PRIMARY KEY (symbol, timeframe, bar_time))""",
    """CREATE TABLE IF NOT EXISTS signals (
         id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL,
         direction TEXT NOT NULL, entry REAL, sl REAL, tp REAL, confidence INTEGER,
         outcome TEXT DEFAULT 'OPEN', closed_ts REAL, last_bar TEXT,
         entry_bar TEXT, ai_verdict TEXT, ai_confidence INTEGER)""",
    """CREATE TABLE IF NOT EXISTS ai_reviews (
         ts REAL, model TEXT, verdict TEXT, confidence INTEGER,
         reasons TEXT, risk_note TEXT, latency_ms INTEGER)""",
]

def db_backend():
    return "turso" if (TURSO_DATABASE_URL and TURSO_AUTH_TOKEN) else "sqlite"

def _turso_endpoint():
    host = TURSO_DATABASE_URL
    if host.startswith("libsql://"):
        host = "https://" + host[len("libsql://"):]
    elif host.startswith("http://"):
        host = "https://" + host[len("http://"):]
    return host.rstrip("/") + "/v2/pipeline"

def _turso_arg(value):
    if value is None:
        return {"type": "null"}
    if isinstance(value, bool):
        return {"type": "integer", "value": str(int(value))}
    if isinstance(value, int):
        return {"type": "integer", "value": str(value)}
    if isinstance(value, float):
        return {"type": "float", "value": value}
    return {"type": "text", "value": str(value)}

def _turso_cell(cell):
    kind, raw = cell.get("type"), cell.get("value")
    if kind == "null" or raw is None:
        return None
    if kind == "integer":
        return int(raw)
    if kind == "float":
        return float(raw)
    return raw

def _turso_run(statements):
    """Send all statements in a single pipeline request."""
    body = {"requests": [{"type": "execute",
                          "stmt": {"sql": sql, "args": [_turso_arg(p) for p in params]}}
                         for sql, params in statements]}
    body["requests"].append({"type": "close"})
    r = requests.post(_turso_endpoint(), json=body, timeout=15,
                      headers={"Authorization": f"Bearer {TURSO_AUTH_TOKEN}",
                               "Content-Type": "application/json"})
    if r.status_code != 200:
        raise RuntimeError(f"Turso HTTP {r.status_code}: {r.text[:200]}")
    rows = []
    for result in r.json().get("results", []):
        if result.get("type") != "ok":
            raise RuntimeError(f"Turso error: {result}")
        block = (result.get("response") or {}).get("result") or {}
        for row in block.get("rows", []):
            rows.append([_turso_cell(c) for c in row])
    return rows

def db_run(statements, fetch=False):
    """Execute (sql, params) pairs. Never raises: storage is best-effort."""
    try:
        if db_backend() == "turso":
            return _turso_run(statements)
        rows = []
        with sqlite3.connect(DB_FILE) as conn:
            for sql, params in statements:
                cur = conn.execute(sql, params)
                if fetch:
                    rows = [list(r) for r in cur.fetchall()]
            conn.commit()
        return rows
    except Exception as e:
        print(f"DB error: {e}")
        return []

def db_init():
    db_run([(sql, ()) for sql in DB_SCHEMA])
    # Deployments that predate entry_bar keep their rows; add the column in place.
    columns = db_run([("PRAGMA table_info(signals)", ())], fetch=True)
    if columns and "entry_bar" not in [row[1] for row in columns]:
        db_run([("ALTER TABLE signals ADD COLUMN entry_bar TEXT", ())])
    print(f"DB: {db_backend()} ({TURSO_DATABASE_URL if db_backend() == 'turso' else DB_FILE})")

def db_save_bars(candles, timeframe):
    """Upsert bars so a still-forming candle is corrected on the next scan."""
    if not candles:
        return
    db_run([("INSERT OR REPLACE INTO bars (symbol, timeframe, bar_time, open, high, low, close) "
             "VALUES (?,?,?,?,?,?,?)",
             (SYMBOL, timeframe, c["time"], c["open"], c["high"], c["low"], c["close"]))
            for c in candles])

def db_save_signal(rec):
    db_run([("INSERT INTO signals (ts, direction, entry, sl, tp, confidence, outcome, "
             "closed_ts, last_bar, entry_bar, ai_verdict, ai_confidence) "
             "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
             (rec["ts"], rec["direction"], rec.get("entry"), rec.get("sl"), rec.get("tp"),
              rec.get("confidence"), rec.get("outcome", "OPEN"), rec.get("closed_ts"),
              rec.get("last_bar"), rec.get("entry_bar"), rec.get("ai_verdict"),
              rec.get("ai_confidence")))])

def db_update_signal(rec):
    db_run([("UPDATE signals SET outcome=?, closed_ts=?, last_bar=?, entry_bar=? "
             "WHERE ts=? AND direction=?",
             (rec.get("outcome"), rec.get("closed_ts"), rec.get("last_bar"),
              rec.get("entry_bar"), rec["ts"], rec["direction"]))])

DB_SIGNAL_COLUMNS = ("ts", "direction", "entry", "sl", "tp", "confidence",
                     "outcome", "closed_ts", "last_bar", "entry_bar",
                     "ai_verdict", "ai_confidence")

def db_load_journal(limit=None):
    rows = db_run([("SELECT ts, direction, entry, sl, tp, confidence, outcome, closed_ts, "
                    "last_bar, entry_bar, ai_verdict, ai_confidence "
                    "FROM signals ORDER BY ts DESC LIMIT ?",
                    (limit or JOURNAL_MAX,))], fetch=True)
    return [dict(zip(DB_SIGNAL_COLUMNS, row)) for row in reversed(rows)]

def db_save_ai_review(rec, model, opinion, latency_ms):
    db_run([("INSERT INTO ai_reviews (ts, model, verdict, confidence, reasons, risk_note, latency_ms) "
             "VALUES (?,?,?,?,?,?,?)",
             (rec["ts"], model, opinion["verdict"], opinion["confidence"],
              json.dumps(opinion["reasons"]), opinion["risk_note"], latency_ms))])

def db_migrate_journal(records):
    """Seed the DB from the JSON journal once so earlier history survives."""
    if not records:
        return 0
    db_run([("INSERT INTO signals (ts, direction, entry, sl, tp, confidence, outcome, "
             "closed_ts, last_bar, ai_verdict, ai_confidence) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
             (r["ts"], r["direction"], r.get("entry"), r.get("sl"), r.get("tp"),
              r.get("confidence"), r.get("outcome", "OPEN"), r.get("closed_ts"),
              r.get("last_bar"), r.get("ai_verdict"), r.get("ai_confidence")))
            for r in records])
    return len(records)

# ============================================
# SIGNAL JOURNAL  (every alert, and how it resolved)
# ============================================
def load_journal():
    """Alerts sent so far. Missing or corrupt file = empty journal."""
    try:
        with open(JOURNAL_FILE, "r") as fh:
            raw = json.load(fh)
        return [r for r in raw.get("signals", [])
                if r.get("direction") in ("BUY", "SELL", "LONG", "SHORT")]
    except FileNotFoundError:
        return []
    except Exception as e:
        print(f"Journal load error ({JOURNAL_FILE}): {e} — starting empty.")
        return []

def save_journal(journal):
    tmp = f"{JOURNAL_FILE}.tmp"
    try:
        with open(tmp, "w") as fh:
            json.dump({"signals": journal[-JOURNAL_MAX:]}, fh)
        os.replace(tmp, JOURNAL_FILE)
    except Exception as e:
        print(f"Journal save error ({JOURNAL_FILE}): {e}")

def plan_prices(sig):
    """The exact SL/TP quoted in the alert."""
    if sig["signal"] == "LONG":
        return (round(sig["price"] - sig["sl_dollars"], 2),
                round(sig["price"] + sig["tp_dollars"], 2))
    return (round(sig["price"] + sig["sl_dollars"], 2),
            round(sig["price"] - sig["tp_dollars"], 2))

def record_signal(journal, sig, opinion=None, entry_bar=None):
    sl, tp = plan_prices(sig)
    journal.append({
        "ts": time.time(), "direction": sig["signal"], "entry": sig["price"],
        "sl": sl, "tp": tp, "confidence": sig["confidence"],
        "outcome": "OPEN", "closed_ts": None, "last_bar": None, "entry_bar": entry_bar,
        "ai_verdict": (opinion or {}).get("verdict"),
        "ai_confidence": (opinion or {}).get("confidence"),
    })
    save_journal(journal)
    db_save_signal(journal[-1])
    return journal

def evaluate_journal(journal, candles):
    """Resolve OPEN records by walking only the bars that printed after they were sent.
    A bar that touches both levels is counted as a loss."""
    if not candles:
        return journal
    changed = False
    for rec in journal:
        if rec.get("outcome") != "OPEN":
            continue
        # Start at the bar that was forming when the alert went out. Walking the
        # whole window replays pre-entry bars and marks almost every trade as an
        # instant loss, so a record without an entry bar is left untouched.
        last = rec.get("last_bar") or rec.get("entry_bar")
        if not last:
            continue
        for c in candles:
            if c["time"] <= last:
                continue
            last = c["time"]
            if rec["direction"] in ("BUY", "LONG"):
                if c["low"] <= rec["sl"]:
                    rec["outcome"] = "SL"
                elif c["high"] >= rec["tp"]:
                    rec["outcome"] = "TP"
            else:
                if c["high"] >= rec["sl"]:
                    rec["outcome"] = "SL"
                elif c["low"] <= rec["tp"]:
                    rec["outcome"] = "TP"
            if rec["outcome"] != "OPEN":
                rec["closed_ts"] = time.time()
                break
        rec["last_bar"] = last
        db_update_signal(rec)
        changed = True
    if changed:
        save_journal(journal)
    return journal

def score_band(confidence):
    if confidence >= 85:
        return "85+"
    if confidence >= 75:
        return "75-84"
    return "65-74"

def hit_rate(bucket):
    total = bucket["tp"] + bucket["sl"]
    return None if not total else round(100 * bucket["tp"] / total)

def journal_stats(journal, recent=8):
    """Hit rates the AI is given as context, so 'learning' means data, not vibes."""
    stats = {"closed": 0, "tp": 0, "sl": 0, "open": 0,
             "by_direction": {}, "by_band": {}, "recent": []}
    closed = []
    for rec in journal:
        if rec.get("outcome") == "OPEN":
            stats["open"] += 1
            continue
        if rec.get("outcome") not in ("TP", "SL"):
            continue
        closed.append(rec)
        stats["closed"] += 1
        won = rec["outcome"] == "TP"
        stats["tp" if won else "sl"] += 1
        direction = "BUY" if rec.get("direction") in ("BUY", "LONG") else "SELL"
        for table, key in ((stats["by_direction"], direction),
                           (stats["by_band"], score_band(rec.get("confidence") or 0))):
            table.setdefault(key, {"tp": 0, "sl": 0})["tp" if won else "sl"] += 1
    for rec in closed[-recent:]:
        direction = "BUY" if rec.get("direction") in ("BUY", "LONG") else "SELL"
        stats["recent"].append(f"{direction} {rec.get('confidence')}% -> {rec['outcome']}")
    return stats

# ============================================
# AI SECOND OPINION  (OpenRouter, advisory only)
# ============================================
AI_SYSTEM_PROMPT = (
    "You are a strict risk reviewer for a gold (XAU/USD) SMC signal bot. "
    "You cannot see charts and cannot predict price; never claim to know what "
    "price will do. Judge only whether this setup is internally consistent and "
    "worth taking, using the structure, the confluence list, and the historical "
    "hit rates provided. Weigh the journal: a low hit rate in the same direction "
    "or confidence band is evidence against the trade. Do not invent levels or "
    "confluences that are not in the input. Reply with JSON only."
)

AI_OPINION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["verdict", "confidence", "reasons", "risk_note"],
    "properties": {
        "verdict": {"type": "string", "enum": ["AGREE", "CAUTION", "REJECT"]},
        "confidence": {"type": "integer", "minimum": 0, "maximum": 100},
        "reasons": {"type": "array", "items": {"type": "string"}, "maxItems": 3},
        "risk_note": {"type": "string"},
    },
}

def ai_second_opinion(sig, candles_5m, candles_15m, stats):
    """Ask the model to review the rule signal. Returns None on any failure —
    the alert has already been sent, so the AI must never block it."""
    if not AI_ENABLED or not sig:
        return None

    def bars(candles, count):
        return [[round(c["open"], 2), round(c["high"], 2),
                 round(c["low"], 2), round(c["close"], 2)]
                for c in candles[-count:]]

    sl, tp = plan_prices(sig)
    payload = {
        "instrument": "XAU/USD",
        "rule_signal": {
            "direction": "BUY" if sig["signal"] == "LONG" else "SELL",
            "entry": sig["price"], "stop_loss": sl, "take_profit": tp,
            "confidence": sig["confidence"], "risk_reward": "1:2",
            "rsi": sig["rsi"], "tf15_bias": sig["tf_bias"],
            "bos": sig["bos_type"], "choch": sig["choch"],
            "order_block": {"high": sig["ob_high"], "low": sig["ob_low"],
                            "price_inside": sig["price_in_ob"]},
            "liquidity": {"equal_highs": sig["eq_highs"], "equal_lows": sig["eq_lows"],
                          "swept_high": sig["swept_high"], "swept_low": sig["swept_low"],
                          "grab": sig["liquidity"]},
            "fvg": sig["fvg"], "pattern": sig["pattern"],
            "confluences": sig["reasons"],
        },
        "recent_bars_5m_ohlc": bars(candles_5m, 20),
        "recent_bars_15m_ohlc": bars(candles_15m, 12),
        "journal": {
            "closed_signals": stats["closed"],
            "take_profit": stats["tp"], "stop_loss": stats["sl"],
            "hit_rate_pct": hit_rate({"tp": stats["tp"], "sl": stats["sl"]}),
            "hit_rate_by_direction": {k: hit_rate(v) for k, v in stats["by_direction"].items()},
            "hit_rate_by_confidence_band": {k: hit_rate(v) for k, v in stats["by_band"].items()},
            "recent_outcomes": stats["recent"],
        },
    }
    body = {
        "model": OPENROUTER_MODEL,
        "messages": [{"role": "system", "content": AI_SYSTEM_PROMPT},
                     {"role": "user", "content": json.dumps(payload)}],
        "temperature": 0.2,
        "max_tokens": 400,
        "response_format": {"type": "json_schema", "json_schema": {
            "name": "signal_opinion", "strict": True, "schema": AI_OPINION_SCHEMA}},
    }
    try:
        r = requests.post("https://openrouter.ai/api/v1/chat/completions",
                          headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}",
                                   "Content-Type": "application/json"},
                          json=body, timeout=AI_TIMEOUT)
        if r.status_code != 200:
            print(f"AI error {r.status_code}: {r.text[:200]}")
            return None
        content = r.json()["choices"][0]["message"]["content"]
        opinion = json.loads(content)
    except Exception as e:
        print(f"AI call failed: {e}")
        return None
    return validate_opinion(opinion)

def validate_opinion(opinion):
    """Schema-valid JSON can still be nonsense, so check the values that matter."""
    if not isinstance(opinion, dict):
        return None
    verdict = str(opinion.get("verdict", "")).upper()
    if verdict not in ("AGREE", "CAUTION", "REJECT"):
        print(f"AI opinion discarded — unknown verdict {verdict!r}")
        return None
    try:
        confidence = int(opinion.get("confidence"))
    except (TypeError, ValueError):
        print("AI opinion discarded — confidence is not a number")
        return None
    if not 0 <= confidence <= 100:
        print(f"AI opinion discarded — confidence {confidence} out of range")
        return None
    reasons = [str(x).strip() for x in (opinion.get("reasons") or []) if str(x).strip()][:3]
    return {"verdict": verdict, "confidence": confidence, "reasons": reasons,
            "risk_note": str(opinion.get("risk_note") or "").strip()[:300]}

def send_ai_opinion(sig, opinion, stats):
    icons    = {"AGREE": "✅", "CAUTION": "⚠️", "REJECT": "⛔"}
    direction = "BUY" if sig["signal"] == "LONG" else "SELL"
    lines = [
        f"🤖 <b>AI Second Opinion</b> <i>({OPENROUTER_MODEL.split('/')[-1]})</i>",
        "━━━━━━━━━━━━━━━━━━━━",
        f"📊 Rule signal: <b>{direction}</b> @ {sig['price']:.2f} | rule conf {sig['confidence']}%",
        f"{icons.get(opinion['verdict'], 'ℹ️')} Verdict: <b>{opinion['verdict']}</b> ({opinion['confidence']}%)",
    ]
    if opinion["reasons"]:
        lines.append("📝 <b>Reasons:</b>")
        lines.extend(f"  • {r}" for r in opinion["reasons"])
    if opinion["risk_note"]:
        lines.append(f"⚠️ <b>Risk:</b> {opinion['risk_note']}")
    if stats["closed"]:
        rate = hit_rate({"tp": stats["tp"], "sl": stats["sl"]})
        lines.append(f"📈 <b>Journal:</b> {stats['closed']} closed — "
                     f"{stats['tp']} TP / {stats['sl']} SL ({rate}% hit)")
    else:
        lines.append("📈 <b>Journal:</b> no closed signals yet — no hit-rate context.")
    lines.append("\n<i>AI cannot change entry/SL/TP. Second opinion, not an order.</i>")
    send_telegram("\n".join(lines))

# ============================================
# TELEGRAM
# ============================================
def send_telegram(message):
    url     = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}
    try:
        r = requests.post(url, json=payload, timeout=10)
        print(f"Telegram: {r.status_code}")
    except Exception as e:
        print(f"Telegram error: {e}")

def send_photo(photo_path, caption=""):
    url  = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto"
    data = {"chat_id": TELEGRAM_CHAT_ID, "caption": caption, "parse_mode": "HTML"}
    try:
        with open(photo_path, "rb") as fh:
            r = requests.post(url, data=data, files={"photo": fh}, timeout=30)
        print(f"Telegram photo: {r.status_code}")
        if r.status_code != 200:
            print(f"Telegram photo error: {r.text[:200]}")
        return r.status_code == 200
    except Exception as e:
        print(f"Telegram photo error: {e}")
        return False

# ============================================
# MANUAL SCAN  (/scan in Telegram)
# ============================================
# Shared with the command handlers, which run on the listener thread.
journal_cache = []
stats_cache   = None
alerts_paused = False

def cmd_help():
    send_telegram(
        "<b>Commands</b>\n"
        "/scan — scan now (2 API requests)\n"
        "/price — price, 15M bias and RSI (2 API requests)\n"
        "/status — market, alerts, storage and AI state\n"
        "/stats — hit rate and AI calibration\n"
        "/journal — last 5 alerts with outcomes\n"
        "/settings — active configuration\n"
        "/pause — stop sending alerts\n"
        "/resume — resume alerts\n"
        "/help — this list"
    )

def cmd_status():
    journal = journal_cache or []
    st      = stats_cache or journal_stats(journal)
    rate    = hit_rate({"tp": st["tp"], "sl": st["sl"]})
    et      = datetime.now(NY)
    send_telegram(
        f"📊 <b>Status</b>\n"
        f"🕐 {datetime.now(IST).strftime('%d %b %H:%M')} IST | {et.strftime('%H:%M')} ET\n"
        f"🏦 Market: <b>{'closed' if market_closed(et) else 'open'}</b>\n"
        f"📡 Data: {', '.join(provider_names()) or 'NONE'}\n"
        f"🗄 DB: {db_backend()}\n"
        f"🤖 AI: {'on — ' + OPENROUTER_MODEL.split('/')[-1] if AI_ENABLED else 'off'}\n"
        f"🔔 Alerts: {'paused' if alerts_paused else 'active'}\n"
        f"🎯 Signals (last {SIGNAL_WINDOW_HOURS:g}h): {len(prune_state(signals_sent))}\n"
        f"📈 Journal: {st['closed']} closed, {st['open']} open"
        + (f", {rate}% hit" if rate is not None else "")
    )

def ai_calibration(journal):
    """Hit rate per AI verdict — the only honest way to tell if the AI helps."""
    buckets = {}
    for rec in journal:
        if rec.get("outcome") not in ("TP", "SL"):
            continue
        key = rec.get("ai_verdict") or "no AI"
        buckets.setdefault(key, {"tp": 0, "sl": 0})["tp" if rec["outcome"] == "TP" else "sl"] += 1
    return buckets

def cmd_stats():
    journal = journal_cache or []
    st      = stats_cache or journal_stats(journal)
    if not st["closed"]:
        send_telegram("📈 <b>Stats</b>\nBelum ada sinyal tertutup — belum ada yang bisa diukur.")
        return
    rate  = hit_rate({"tp": st["tp"], "sl": st["sl"]})
    lines = [f"📈 <b>Stats</b> — {st['closed']} closed",
             f"✅ TP {st['tp']} | ❌ SL {st['sl']} | hit rate <b>{rate}%</b>"]
    for name, bucket in sorted(st["by_direction"].items()):
        total = bucket["tp"] + bucket["sl"]
        lines.append(f"• {name}: {bucket['tp']}/{total} ({hit_rate(bucket)}%)")
    for band, bucket in sorted(st["by_band"].items()):
        total = bucket["tp"] + bucket["sl"]
        lines.append(f"• conf {band}: {bucket['tp']}/{total} ({hit_rate(bucket)}%)")
    calibration = ai_calibration(journal)
    if calibration:
        lines.append("🤖 <b>AI verdicts:</b>")
        for verdict, bucket in sorted(calibration.items()):
            total = bucket["tp"] + bucket["sl"]
            lines.append(f"• {verdict}: {bucket['tp']}/{total} ({hit_rate(bucket)}%)")
    send_telegram("\n".join(lines))

def cmd_journal():
    journal = journal_cache or []
    if not journal:
        send_telegram("🗂 <b>Journal</b>\nKosong — belum ada sinyal tercatat.")
        return
    icons = {"TP": "✅", "SL": "❌", "OPEN": "⏳"}
    lines = ["🗂 <b>Last alerts</b>"]
    for rec in journal[-5:][::-1]:
        when      = datetime.fromtimestamp(rec["ts"], IST).strftime("%d %b %H:%M")
        direction = "BUY" if rec.get("direction") in ("BUY", "LONG") else "SELL"
        ai        = f" | AI {rec['ai_verdict']}" if rec.get("ai_verdict") else ""
        lines.append(f"{icons.get(rec.get('outcome'), '•')} {when} {direction} "
                     f"@ {rec.get('entry')} conf {rec.get('confidence')}%{ai}")
    send_telegram("\n".join(lines))

def cmd_price():
    send_telegram("🔄 Fetching price...")
    candles    = get_candles("5min", 60)
    candles_15 = get_candles("15min", 60)
    if not candles:
        send_telegram("❌ Gagal mengambil harga saat ini.")
        return
    bias = "n/a"
    if candles_15:
        tf_bias, bos_type, _ = analyze_structure_bos(candles_15)
        bias = f"{tf_bias}{' / ' + bos_type if bos_type else ''}"
    send_telegram(
        f"💰 <b>{SYMBOL}</b>: {candles[-1]['close']:.2f}\n"
        f"📈 15M bias: {bias}\n"
        f"📉 RSI (5M): {calculate_rsi(candles):.1f}\n"
        f"🕐 {datetime.now(IST).strftime('%H:%M')} IST | {get_session()}"
    )

def cmd_settings():
    cap = ("none" if not QUOTA_ENABLED
           else f"{MAX_TRADES_PER_DAY} per {SIGNAL_WINDOW_HOURS:g}h")
    send_telegram(
        f"⚙️ <b>Settings</b>\n"
        f"Instrument: {SYMBOL}\n"
        f"Data: {', '.join(provider_names()) or 'NONE'} (DATA_PROVIDER={DATA_PROVIDER})\n"
        f"Scan: every 5m | Timeframes: 5M + 15M\n"
        f"Min confidence: {MIN_CONFIDENCE}%\n"
        f"Signal cap: {cap}\n"
        f"Same-direction cooldown: {SAME_DIRECTION_COOLDOWN // 60}m\n"
        f"Paper mode: {PAPER_MODE}\n"
        f"AI: {OPENROUTER_MODEL if AI_ENABLED else 'off'}\n"
        f"DB: {db_backend()}"
    )

def cmd_pause():
    global alerts_paused
    alerts_paused = True
    send_telegram("⏸ <b>Alerts paused</b>\nScanning continues but nothing is sent. /resume to undo.")

def cmd_resume():
    global alerts_paused
    alerts_paused = False
    send_telegram("▶️ <b>Alerts resumed.</b>")

COMMAND_HANDLERS = {
    "/help":     cmd_help,
    "/status":   cmd_status,
    "/stats":    cmd_stats,
    "/journal":  cmd_journal,
    "/price":    cmd_price,
    "/settings": cmd_settings,
    "/pause":    cmd_pause,
    "/resume":   cmd_resume,
}

def handle_command_update(update, scan_event):
    """Route a Telegram command from the configured chat. Returns True when a
    scan was requested."""
    message = update.get("message") or {}
    text    = (message.get("text") or "").strip()
    chat_id = str((message.get("chat") or {}).get("id", ""))

    if not text.startswith("/"):
        return False
    if chat_id != str(TELEGRAM_CHAT_ID):
        print(f"Ignored command from unknown chat {chat_id}")
        return False

    command = text.split()[0].split("@")[0].lower()
    if command == "/scan":
        scan_event.set()
        send_telegram("🔍 <b>Manual scan requested</b>\nScanning now...")
        print("Manual scan requested via Telegram.")
        return True

    handler = COMMAND_HANDLERS.get(command)
    if not handler:
        send_telegram(f"❓ Unknown command: <code>{command}</code>\nSend /help.")
        return False
    try:
        handler()
    except Exception as e:
        print(f"Command {command} failed: {e}")
        send_telegram(f"❌ {command} failed: {e}")
    return False

def command_listener(scan_event):
    """Poll Telegram for /scan while the main loop runs on this thread."""
    token = TELEGRAM_TOKEN
    url   = f"https://api.telegram.org/bot{token}/getUpdates"
    offset = None

    # getUpdates fails with 409 if a webhook is registered. This bot never sets
    # one, so this is a no-op unless something else registered one.
    try:
        requests.post(f"https://api.telegram.org/bot{token}/deleteWebhook",
                      json={"drop_pending_updates": False}, timeout=10)
    except Exception as e:
        print(f"Command listener setup error: {e}")

    while True:
        try:
            params = {"timeout": 0, "allowed_updates": '["message"]'}
            if offset is not None:
                params["offset"] = offset
            r    = requests.get(url, params=params, timeout=15)
            data = r.json()
            for update in data.get("result", []):
                offset = update["update_id"] + 1
                handle_command_update(update, scan_event)
        except Exception as e:
            print(f"Command listener error: {e}")
        time.sleep(5)

# ============================================
# FETCH CANDLES
# ============================================
OANDA_HOST = ("https://api-fxlive.oanda.com" if OANDA_ENV == "live"
              else "https://api-fxpractice.oanda.com")
OANDA_GRANULARITY = {"1min": "M1", "5min": "M5", "15min": "M15",
                     "30min": "M30", "1h": "H1", "4h": "H4", "1day": "D"}

def provider_names():
    """Configured providers, in the order they are tried."""
    names = []
    if DATA_PROVIDER == "oanda" and OANDA_API_KEY and OANDA_ACCOUNT_ID:
        names.append(f"OANDA {OANDA_INSTRUMENT} ({OANDA_ENV})")
    if TWELVEDATA_API_KEY:
        names.append("TwelveData")
    return names

def get_candles_oanda(interval="5min", outputsize=100):
    """Mid-price candles from OANDA v20. Incomplete (still forming) bars are dropped."""
    granularity = OANDA_GRANULARITY.get(interval)
    if not granularity:
        print(f"OANDA: unsupported interval {interval}")
        return None
    url     = f"{OANDA_HOST}/v3/instruments/{OANDA_INSTRUMENT}/candles"
    params  = {"granularity": granularity, "count": outputsize, "price": "M"}
    headers = {"Authorization": f"Bearer {OANDA_API_KEY}"}
    try:
        r    = requests.get(url, params=params, headers=headers, timeout=15)
        data = r.json()
        if "candles" not in data:
            print(f"OANDA API Error: {data}")
            return None
        candles = []
        for c in data["candles"]:
            if not c.get("complete", True):
                continue
            mid = c["mid"]
            candles.append({
                "time": c["time"], "open": float(mid["o"]), "high": float(mid["h"]),
                "low": float(mid["l"]), "close": float(mid["c"])
            })
        return candles or None
    except Exception as e:
        print(f"OANDA fetch error: {e}")
        return None

def get_candles_twelvedata(interval="5min", outputsize=100):
    url    = "https://api.twelvedata.com/time_series"
    params = {
        "symbol": SYMBOL, "interval": interval,
        "outputsize": outputsize, "apikey": TWELVEDATA_API_KEY, "format": "JSON",
        # TwelveData otherwise stamps bars in the account timezone, which put
        # every stored bar ~11h away from the real UTC time.
        "timezone": "UTC",
    }
    try:
        r    = requests.get(url, params=params, timeout=15)
        data = r.json()
        if "values" not in data:
            print(f"API Error: {data}")
            return None
        candles = []
        for c in reversed(data["values"]):
            candles.append({
                "time": c["datetime"], "open": float(c["open"]),
                "high": float(c["high"]), "low": float(c["low"]),
                "close": float(c["close"])
            })
        return candles
    except Exception as e:
        print(f"Candle fetch error: {e}")
        return None

def get_candles(interval="5min", outputsize=100):
    """Ask each configured provider in turn and return the first usable series."""
    providers = []
    if DATA_PROVIDER == "oanda" and OANDA_API_KEY and OANDA_ACCOUNT_ID:
        providers.append(("OANDA", get_candles_oanda))
    if TWELVEDATA_API_KEY:
        providers.append(("TwelveData", get_candles_twelvedata))

    if not providers:
        print("No market data provider configured "
              "(set OANDA_API_KEY + OANDA_ACCOUNT_ID, or TWELVEDATA_API_KEY).")
        return None

    for name, fetch in providers:
        candles = fetch(interval, outputsize)
        if candles:
            return candles
        print(f"{name} returned no candles — trying the next provider.")
    return None

# ============================================
# SWING HIGH / LOW DETECTION
# ============================================
def find_swing_highs(candles, lookback=3):
    swings = []
    for i in range(lookback, len(candles) - lookback):
        if all(candles[i]["high"] >= candles[i-j]["high"] and
               candles[i]["high"] >= candles[i+j]["high"]
               for j in range(1, lookback+1)):
            swings.append((i, candles[i]["high"]))
    return swings

def find_swing_lows(candles, lookback=3):
    swings = []
    for i in range(lookback, len(candles) - lookback):
        if all(candles[i]["low"] <= candles[i-j]["low"] and
               candles[i]["low"] <= candles[i+j]["low"]
               for j in range(1, lookback+1)):
            swings.append((i, candles[i]["low"]))
    return swings

# ============================================
# MARKET STRUCTURE — BOS & CHoCH
# ============================================
def analyze_structure_bos(candles):
    """
    Returns (bias, bos_type, choch)
    bias    = "BULLISH" | "BEARISH" | "NEUTRAL"
    bos_type = "BOS" | "CHoCH" | None
    """
    if len(candles) < 20:
        return "NEUTRAL", None, False

    swing_highs = find_swing_highs(candles[-60:], lookback=3)
    swing_lows  = find_swing_lows(candles[-60:],  lookback=3)

    if len(swing_highs) < 2 or len(swing_lows) < 2:
        return "NEUTRAL", None, False

    curr_price = candles[-1]["close"]
    last_sh    = swing_highs[-1][1]
    last_sl    = swing_lows[-1][1]
    prev_sh    = swing_highs[-2][1]
    prev_sl    = swing_lows[-2][1]

    # EMA trend bias
    closes   = [c["close"] for c in candles]
    ema_fast = sum(closes[-10:]) / 10
    ema_slow = sum(closes[-30:]) / 30
    ema_bias = "BULLISH" if ema_fast > ema_slow else "BEARISH"

    # BOS: price breaks above last swing high = bullish BOS
    #      price breaks below last swing low  = bearish BOS
    bullish_bos = curr_price > last_sh
    bearish_bos = curr_price < last_sl

    # CHoCH: structural reversal
    # Bullish CHoCH: was bearish trend (lower highs), now breaks above last swing high
    was_bearish  = last_sh < prev_sh and last_sl < prev_sl
    was_bullish  = last_sh > prev_sh and last_sl > prev_sl
    bullish_choch = was_bearish and bullish_bos
    bearish_choch = was_bullish and bearish_bos

    if bullish_choch:
        return "BULLISH", "CHoCH", True
    elif bearish_choch:
        return "BEARISH", "CHoCH", True
    elif bullish_bos and ema_bias == "BULLISH" and last_sh > prev_sh:
        return "BULLISH", "BOS", False
    elif bearish_bos and ema_bias == "BEARISH" and last_sl < prev_sl:
        return "BEARISH", "BOS", False
    elif ema_bias == "BULLISH" and last_sh > prev_sh and last_sl > prev_sl:
        return "BULLISH", None, False
    elif ema_bias == "BEARISH" and last_sh < prev_sh and last_sl < prev_sl:
        return "BEARISH", None, False
    else:
        return "NEUTRAL", None, False

# ============================================
# ORDER BLOCK DETECTION
# ============================================
def detect_order_block(candles, bias):
    """
    Bullish OB: last bearish (red) candle before a strong bullish impulse
    Bearish OB: last bullish (green) candle before a strong bearish impulse
    Returns (ob_high, ob_low, price_in_ob) or (None, None, False)
    """
    if len(candles) < 10:
        return None, None, False

    curr_price = candles[-1]["close"]

    if bias == "BULLISH":
        # Find last bearish candle followed by bullish impulse
        for i in range(len(candles)-3, max(len(candles)-20, 3), -1):
            c = candles[i]
            # Bearish candle
            if c["close"] < c["open"]:
                # Check if next 2 candles moved up strongly (impulse)
                next_closes = [candles[i+j]["close"] for j in range(1, 3) if i+j < len(candles)]
                if next_closes and max(next_closes) > c["high"]:
                    ob_high = c["high"]
                    ob_low  = c["low"]
                    price_in_ob = ob_low <= curr_price <= ob_high
                    return ob_high, ob_low, price_in_ob

    elif bias == "BEARISH":
        # Find last bullish candle followed by bearish impulse
        for i in range(len(candles)-3, max(len(candles)-20, 3), -1):
            c = candles[i]
            # Bullish candle
            if c["close"] > c["open"]:
                # Check if next 2 candles moved down strongly (impulse)
                next_closes = [candles[i+j]["close"] for j in range(1, 3) if i+j < len(candles)]
                if next_closes and min(next_closes) < c["low"]:
                    ob_high = c["high"]
                    ob_low  = c["low"]
                    price_in_ob = ob_low <= curr_price <= ob_high
                    return ob_high, ob_low, price_in_ob

    return None, None, False

# ============================================
# EQUAL HIGHS / EQUAL LOWS (LIQUIDITY ZONES)
# ============================================
def detect_liquidity_zones(candles):
    """
    Equal highs = buy-side liquidity (stops sitting above)
    Equal lows  = sell-side liquidity (stops sitting below)
    Returns (equal_highs, equal_lows, swept_high, swept_low)
    """
    if len(candles) < 10:
        return False, False, False, False

    recent      = candles[-30:]
    curr_price  = candles[-1]["close"]
    highs       = [c["high"] for c in recent[:-2]]
    lows        = [c["low"]  for c in recent[:-2]]
    last_high   = candles[-1]["high"]
    last_low    = candles[-1]["low"]

    # Find equal highs (within tolerance)
    equal_highs = False
    swept_high  = False
    for i in range(len(highs)):
        for j in range(i+1, len(highs)):
            if abs(highs[i] - highs[j]) <= EQUAL_LEVEL_TOL:
                equal_highs = True
                # Swept = current candle wick went above those equal highs
                if last_high > max(highs[i], highs[j]):
                    swept_high = True

    # Find equal lows (within tolerance)
    equal_lows = False
    swept_low  = False
    for i in range(len(lows)):
        for j in range(i+1, len(lows)):
            if abs(lows[i] - lows[j]) <= EQUAL_LEVEL_TOL:
                equal_lows = True
                if last_low < min(lows[i], lows[j]):
                    swept_low = True

    return equal_highs, equal_lows, swept_high, swept_low

# ============================================
# IMPROVED FVG WITH SIZE FILTER
# ============================================
def detect_fvg(candles):
    """
    Checks last 10 candle triplets for any unfilled FVG
    with minimum size filter to remove noise.
    """
    if len(candles) < 3:
        return None, 0

    # Check multiple recent candle triplets, not just the last 3
    for i in range(len(candles)-3, max(len(candles)-10, 0), -1):
        c1 = candles[i]
        c2 = candles[i+1]
        c3 = candles[i+2]

        # Bullish FVG: gap between c1 high and c3 low
        if c1["high"] < c3["low"]:
            gap_size = c3["low"] - c1["high"]
            if gap_size >= MIN_FVG_SIZE:
                # Check if still unfilled (current price hasn't gone back into gap)
                curr = candles[-1]["close"]
                if curr > c1["high"]:  # price still above the gap
                    return "BULLISH_FVG", 1

        # Bearish FVG: gap between c1 low and c3 high
        if c1["low"] > c3["high"]:
            gap_size = c1["low"] - c3["high"]
            if gap_size >= MIN_FVG_SIZE:
                curr = candles[-1]["close"]
                if curr < c1["low"]:  # price still below the gap
                    return "BEARISH_FVG", 1

    return None, 0

# ============================================
# IMPROVED LIQUIDITY GRAB
# ============================================
def detect_liquidity_grab(candles):
    """
    Spike above recent high then close back below = bearish grab
    Spike below recent low then close back above  = bullish grab
    Uses dollar-based wick size instead of body ratio
    to handle doji candles correctly.
    """
    if len(candles) < 10:
        return None, 0

    recent       = candles[-10:]
    prev         = candles[-2]
    curr         = candles[-1]
    recent_highs = [c["high"] for c in recent[:-2]]
    recent_lows  = [c["low"]  for c in recent[:-2]]

    if not recent_highs or not recent_lows:
        return None, 0

    max_high = max(recent_highs)
    min_low  = min(recent_lows)
    grab     = None
    score    = 0

    # Bearish grab: wick above recent high, closed back below
    if prev["high"] > max_high:
        wick_size = prev["high"] - max(prev["open"], prev["close"])
        if wick_size >= 1.0:  # minimum $1 wick to count
            if curr["close"] < curr["open"]:
                grab  = "BEARISH_GRAB"
                score = 2

    # Bullish grab: wick below recent low, closed back above
    if prev["low"] < min_low:
        wick_size = min(prev["open"], prev["close"]) - prev["low"]
        if wick_size >= 1.0:  # minimum $1 wick to count
            if curr["close"] > curr["open"]:
                grab  = "BULLISH_GRAB"
                score = 2

    return grab, score

# ============================================
# CANDLESTICK PATTERN
# ============================================
def check_candle_pattern(candles):
    if len(candles) < 2:
        return None, 0

    c2 = candles[-2]
    c3 = candles[-1]

    if (c2["open"] < c2["close"] and c3["open"] > c3["close"] and
            c3["open"] >= c2["close"] and c3["close"] <= c2["open"]):
        return "BEARISH_ENGULF", 2
    elif (c2["open"] > c2["close"] and c3["open"] < c3["close"] and
            c3["open"] <= c2["close"] and c3["close"] >= c2["open"]):
        return "BULLISH_ENGULF", 2
    elif (c3["high"] - max(c3["open"], c3["close"]) >
            2 * abs(c3["open"] - c3["close"]) and
            abs(c3["open"] - c3["close"]) > 0):
        return "SHOOTING_STAR", 1
    elif (min(c3["open"], c3["close"]) - c3["low"] >
            2 * abs(c3["open"] - c3["close"]) and
            abs(c3["open"] - c3["close"]) > 0):
        return "HAMMER", 1

    return None, 0

# ============================================
# RSI
# ============================================
def calculate_rsi(candles, period=14):
    if len(candles) < period + 1:
        return 50
    closes   = [c["close"] for c in candles[-(period+1):]]
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i-1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    avg_g = sum(gains) / period
    avg_l = sum(losses) / period
    if avg_l == 0:
        return 100
    return round(100 - (100 / (1 + avg_g / avg_l)), 2)

def calculate_atr(candles, period=14):
    """Average true range of the last `period` bars; None when history is short."""
    if len(candles) < period + 1:
        return None
    ranges = []
    for prev, cur in zip(candles[-period - 1:-1], candles[-period:]):
        ranges.append(max(cur["high"] - cur["low"],
                          abs(cur["high"] - prev["close"]),
                          abs(cur["low"] - prev["close"])))
    return sum(ranges) / period


# ============================================
# SMART SL PLACEMENT
# ============================================
def calculate_sl_distance(candles, bias, ob_high, ob_low):
    """
    LONG: SL below order block low, or below last swing low
    SHORT: SL above order block high, or above last swing high
    Returns sl_dollars (distance from current price)
    """
    curr = candles[-1]["close"]

    if bias == "LONG":
        if ob_low is not None:
            sl_level = ob_low - 0.50   # just below OB low
        else:
            swing_lows = find_swing_lows(candles[-30:])
            sl_level = swing_lows[-1][1] - 0.50 if swing_lows else curr - 8.0
        return max(curr - sl_level, MIN_SL_DOLLARS)

    elif bias == "SHORT":
        if ob_high is not None:
            sl_level = ob_high + 0.50  # just above OB high
        else:
            swing_highs = find_swing_highs(candles[-30:])
            sl_level = swing_highs[-1][1] + 0.50 if swing_highs else curr + 8.0
        return max(sl_level - curr, MIN_SL_DOLLARS)

    return 8.0

# ============================================
# CALCULATE LOTS
# ============================================
def calculate_lots(sl_dollars):
    if sl_dollars <= 0:
        return 0.01, 0, False
    raw_lots = (RISK_AMOUNT / sl_dollars) * 0.01
    lots     = round(raw_lots, 2)
    if lots < 0.01:
        return 0.01, round(sl_dollars * 1.0, 2), False
    lots        = min(lots, 0.50)
    actual_risk = sl_dollars * (lots / 0.01) * USD_PER_DOLLAR_MOVE_PER_001_LOT
    is_safe     = actual_risk <= RISK_AMOUNT * 2.0
    return lots, round(actual_risk, 2), is_safe

# ============================================
# MAIN SIGNAL ENGINE — FULL SMC STRATEGY
# ============================================
def generate_signal(candles_5m, candles_15m):
    if not candles_5m or not candles_15m:
        return None

    curr_price = candles_5m[-1]["close"]

    # --- 15M HIGHER TIMEFRAME BIAS (non-negotiable gate) ---
    tf_bias, bos_type, choch = analyze_structure_bos(candles_15m)

    # GATE: if 15M is NEUTRAL, no trade — no higher-TF confirmation
    if tf_bias == "NEUTRAL":
        print(f"Price: {curr_price:.2f} | 15M = NEUTRAL — no trade")
        return None

    # --- 5M ANALYSIS ---
    m5_bias, m5_bos, m5_choch = analyze_structure_bos(candles_5m)
    ob_high, ob_low, price_in_ob = detect_order_block(candles_5m, tf_bias)
    eq_highs, eq_lows, swept_high, swept_low = detect_liquidity_zones(candles_5m)
    liquidity, liq_score = detect_liquidity_grab(candles_5m)
    fvg, fvg_score       = detect_fvg(candles_5m)
    pattern, pat_score   = check_candle_pattern(candles_5m)
    rsi                  = calculate_rsi(candles_5m)

    print(f"\n--- ANALYSIS ---")
    print(f"Price:       {curr_price:.2f}")
    print(f"15M Bias:    {tf_bias} | BOS: {bos_type} | CHoCH: {choch}")
    print(f"M5 Bias:     {m5_bias} | BOS: {m5_bos}")
    print(f"Order Block: high={ob_high} low={ob_low} in_ob={price_in_ob}")
    print(f"Eq Highs:    {eq_highs} swept={swept_high}")
    print(f"Eq Lows:     {eq_lows}  swept={swept_low}")
    print(f"Liquidity:   {liquidity}")
    print(f"FVG:         {fvg}")
    print(f"Pattern:     {pattern}")
    print(f"RSI:         {rsi}")

    # --- SCORING ---
    long_score,  long_reasons  = 0, []
    short_score, short_reasons = 0, []

    # 15M structure (highest weight — non-negotiable direction)
    if tf_bias == "BULLISH":
        long_score += 30
        long_reasons.append(f"15M Bullish ({bos_type or 'trend'}{'+ CHoCH' if choch else ''})")
    elif tf_bias == "BEARISH":
        short_score += 30
        short_reasons.append(f"15M Bearish ({bos_type or 'trend'}{'+ CHoCH' if choch else ''})")

    # CHoCH bonus (reversal signal = higher conviction)
    if choch and tf_bias == "BULLISH":
        long_score += 10
        long_reasons.append("15M CHoCH Reversal")
    elif choch and tf_bias == "BEARISH":
        short_score += 10
        short_reasons.append("15M CHoCH Reversal")

    # 5M structure alignment
    if m5_bias == "BULLISH":
        long_score += 15
        long_reasons.append(f"M5 Bullish{' BOS' if m5_bos else ''}")
    elif m5_bias == "BEARISH":
        short_score += 15
        short_reasons.append(f"M5 Bearish{' BOS' if m5_bos else ''}")

    # Order block
    if price_in_ob and tf_bias == "BULLISH":
        long_score += 20
        long_reasons.append("Price in Bullish Order Block")
    elif price_in_ob and tf_bias == "BEARISH":
        short_score += 20
        short_reasons.append("Price in Bearish Order Block")

    # Liquidity sweep (stop hunt then reversal)
    if swept_low and tf_bias == "BULLISH":
        long_score += 15
        long_reasons.append("Equal Lows Swept (Buy-side liquidity taken)")
    if swept_high and tf_bias == "BEARISH":
        short_score += 15
        short_reasons.append("Equal Highs Swept (Sell-side liquidity taken)")

    # Liquidity grab (wick-based)
    if liquidity == "BULLISH_GRAB":
        long_score += 10
        long_reasons.append("Bullish Liquidity Grab")
    elif liquidity == "BEARISH_GRAB":
        short_score += 10
        short_reasons.append("Bearish Liquidity Grab")

    # FVG
    if fvg == "BULLISH_FVG":
        long_score += 10
        long_reasons.append(f"Bullish FVG (≥${MIN_FVG_SIZE})")
    elif fvg == "BEARISH_FVG":
        short_score += 10
        short_reasons.append(f"Bearish FVG (≥${MIN_FVG_SIZE})")

    # Candlestick pattern
    if pattern in ["BULLISH_ENGULF", "HAMMER"]:
        long_score += pat_score * 5
        long_reasons.append(f"Pattern: {pattern}")
    elif pattern in ["BEARISH_ENGULF", "SHOOTING_STAR"]:
        short_score += pat_score * 5
        short_reasons.append(f"Pattern: {pattern}")

    # RSI
    if rsi < 35:
        long_score += 8
        long_reasons.append(f"RSI Oversold ({rsi})")
    elif rsi > 65:
        short_score += 8
        short_reasons.append(f"RSI Overbought ({rsi})")

    print(f"Long: {long_score} | Short: {short_score} | Need: {MIN_CONFIDENCE}+")

    # --- DETERMINE SIGNAL ---
    signal, score, reasons = None, 0, []

    if long_score > short_score and long_score >= MIN_CONFIDENCE and tf_bias == "BULLISH":
        signal  = "LONG"
        score   = min(long_score, 99)
        reasons = long_reasons
    elif short_score > long_score and short_score >= MIN_CONFIDENCE and tf_bias == "BEARISH":
        signal  = "SHORT"
        score   = min(short_score, 99)
        reasons = short_reasons

    if not signal:
        print("No signal.")
        return None

    # Volatility stop. The structural OB stop sat inside gold's 5m noise: over
    # 70 days it lost money before costs, while 1.5x ATR turned the same signals
    # positive in both halves of the sample. Keep the structural stop only as a
    # fallback when there is not enough history for ATR.
    atr = calculate_atr(candles_5m)
    if atr:
        sl_dollars = min(ATR_STOP_MULT * atr, MAX_SL_DOLLARS)
        sl_dollars = max(sl_dollars, MIN_SL_DOLLARS)
    else:
        sl_dollars = calculate_sl_distance(
            candles_5m,
            "LONG" if signal == "LONG" else "SHORT",
            ob_high, ob_low
        )
        sl_dollars = min(sl_dollars, MAX_SL_DOLLARS)
    tp_dollars = sl_dollars * 2

    lots, actual_risk, is_safe = calculate_lots(sl_dollars)

    if not is_safe:
        print(f"Signal rejected: risk ${actual_risk:.2f} too high.")
        return None

    return {
        "signal":        signal,
        "price":         curr_price,
        "sl_dollars":    round(sl_dollars, 2),
        "tp_dollars":    round(tp_dollars, 2),
        "lots":          lots,
        "potential_loss": actual_risk,
        "confidence":    score,
        "reasons":       reasons,
        "rsi":           rsi,
        "tf_bias":       tf_bias,
        "bos_type":      bos_type or "None",
        "choch":         choch,
        "ob_high":       ob_high,
        "ob_low":        ob_low,
        "price_in_ob":   price_in_ob,
        "eq_highs":      eq_highs,
        "eq_lows":       eq_lows,
        "swept_high":    swept_high,
        "swept_low":     swept_low,
        "liquidity":     liquidity or "None",
        "fvg":           fvg or "None",
        "pattern":       pattern or "None"
    }

# ============================================
# FORMAT & SEND SIGNAL
# ============================================
def send_signal(sig, candles=None):
    direction = "🟢 LONG (BUY)" if sig["signal"] == "LONG" else "🔴 SHORT (SELL)"
    emoji     = "📈" if sig["signal"] == "LONG" else "📉"

    if sig["signal"] == "LONG":
        sl_price = sig["price"] - sig["sl_dollars"]
        tp_price = sig["price"] + sig["tp_dollars"]
    else:
        sl_price = sig["price"] + sig["sl_dollars"]
        tp_price = sig["price"] - sig["tp_dollars"]

    caption = (
        f"{direction}\n"
        f"Entry {sig['price']:.2f} | SL {sl_price:.2f} | TP {tp_price:.2f}\n"
        f"{sig['lots']} lot | R:R 1:2 | confidence {sig['confidence']}%"
    )
    chart_path = None
    if build_signal_chart and candles:
        try:
            chart_path = build_signal_chart(sig, candles)
        except Exception as e:
            print(f"Chart error: {e}")
    if chart_path:
        send_photo(chart_path, caption)

    reasons_text = "\n".join([f"  ✅ {r}" for r in sig["reasons"]])
    now_ist      = datetime.now(IST).strftime('%d %b %Y %H:%M IST')
    mode_banner  = (
        "🧪 <b>PAPER SIGNAL — NOT EXECUTED</b>\n<i>Review only.</i>\n\n"
        if PAPER_MODE else "⚡ <b>LIVE SIGNAL</b>\n\n"
    )

    ob_text = (
        f"📦 <b>Order Block:</b> {sig['ob_low']:.2f} - {sig['ob_high']:.2f} "
        f"{'✅ Price inside OB' if sig['price_in_ob'] else ''}\n"
        if sig["ob_high"] else ""
    )
    liq_text = ""
    if sig["swept_high"]:
        liq_text = "💧 <b>Liquidity:</b> Equal Highs swept (stop hunt confirmed)\n"
    elif sig["swept_low"]:
        liq_text = "💧 <b>Liquidity:</b> Equal Lows swept (stop hunt confirmed)\n"

    quota_note = (f"Max {MAX_TRADES_PER_DAY} signals per {SIGNAL_WINDOW_HOURS:g}h"
                  if QUOTA_ENABLED
                  else f"{SAME_DIRECTION_COOLDOWN // 60}min same-direction cooldown only")

    msg = f"""{mode_banner}⚔️ <b>XAUUSD SIGNAL</b> {emoji}
━━━━━━━━━━━━━━━━━━━━
📊 <b>Direction:</b> {direction}
💰 <b>Entry:</b> {sig["price"]:.2f} (Market)
🛑 <b>Stop Loss:</b> {sl_price:.2f} (${sig["sl_dollars"]:.2f} away)
🎯 <b>Take Profit:</b> {tp_price:.2f} (${sig["tp_dollars"]:.2f} away)
📦 <b>Lots:</b> {sig["lots"]}
⚖️ <b>R:R:</b> 1:2
🎯 <b>Confidence:</b> {sig["confidence"]}%
💵 <b>Max Risk:</b> ~${sig["potential_loss"]:.2f}
━━━━━━━━━━━━━━━━━━━━
📋 <b>WHY THIS TRADE:</b>
{reasons_text}

📈 <b>15M Bias:</b> {sig["tf_bias"]} | {sig["bos_type"]}{'+ CHoCH🔄' if sig["choch"] else ''}
{ob_text}{liq_text}📊 <b>FVG:</b> {sig["fvg"]}
🕯 <b>Pattern:</b> {sig["pattern"]}
📉 <b>RSI:</b> {sig["rsi"]}
━━━━━━━━━━━━━━━━━━━━
🕐 {now_ist}
⚠️ <i>{quota_note}. Verify spread before acting.</i>"""

    send_telegram(msg)
    if chart_path:
        try:
            os.remove(chart_path)
        except OSError:
            pass
    print(f"✅ Signal: {sig['signal']} @ {sig['price']} | "
          f"Conf: {sig['confidence']}% | Risk: ${sig['potential_loss']:.2f}")

# ============================================
# SESSION LABEL
# ============================================
def get_session():
    h = datetime.now(IST).hour
    if 5  <= h < 9:  return "Asian Session 🌏"
    if 13 <= h < 18: return "London Session 🇬🇧"
    if 18 <= h < 23: return "New York Session 🗽"
    return "Off Hours 🌙"

# ============================================
# MARKET HOURS
# ============================================
def market_closed(now=None):
    """XAU/USD spot trades Sun 18:00 ET to Fri 17:00 ET, with a daily
    17:00-18:00 ET settlement break. Checked in ET so DST is handled."""
    n = (now or datetime.now(NY)).astimezone(NY)
    if n.weekday() == 4 and n.hour >= 17:   # Friday, after the close
        return True
    if n.weekday() == 5:                    # Saturday
        return True
    if n.weekday() == 6 and n.hour < 18:    # Sunday, before the open
        return True
    return n.hour == 17                     # daily settlement break

# ============================================
# MAIN LOOP
# ============================================
def main():
    global signals_sent, journal_cache, stats_cache

    signals_sent = load_state()

    print("🚀 XAUUSD SMC Bot Starting...")
    print(f"Capital: ${CAPITAL} | Risk/trade: ${RISK_AMOUNT:.2f} | "
          f"Min Confidence: {MIN_CONFIDENCE}%")
    print(f"Paper mode: {PAPER_MODE}")
    print(f"Data: {', '.join(provider_names()) or 'NONE configured'}")
    quota_line = (f"{MAX_TRADES_PER_DAY} signals / {SIGNAL_WINDOW_HOURS:g}h (rolling)"
                  if QUOTA_ENABLED
                  else f"no cap ({SAME_DIRECTION_COOLDOWN // 60}min same-direction cooldown)")
    print(f"Quota: {quota_line} | {len(signals_sent)} loaded from {STATE_FILE}")

    db_init()
    journal = db_load_journal()
    if not journal:
        legacy = load_journal()
        if legacy:
            db_migrate_journal(legacy)
            journal = db_load_journal()
            print(f"Journal: migrated {len(legacy)} records from {JOURNAL_FILE} into the DB")
        if not journal and legacy:
            journal = legacy
            print("Journal: DB empty or unavailable — continuing from the JSON journal.")
    signals_sent = seed_state_from_journal(signals_sent, journal)
    save_state(signals_sent)
    print(f"Journal: {len(journal)} signals loaded | "
          f"Throttle: {len(signals_sent)} recent signals (state + journal)")
    journal_cache, stats_cache = journal, journal_stats(journal)
    print(f"AI: {'on — ' + OPENROUTER_MODEL if AI_ENABLED else 'off (OPENROUTER_API_KEY not set)'}")

    scan_event = threading.Event()
    if TELEGRAM_TOKEN and TELEGRAM_CHAT_ID:
        threading.Thread(target=command_listener, args=(scan_event,), daemon=True).start()
        print(f"Manual scan: send /scan to chat {TELEGRAM_CHAT_ID}.")
    else:
        print("Manual scan: disabled (Telegram credentials missing).")

    send_telegram(
        f"🚀 <b>XAUUSD SMC Bot LIVE</b>\n\n"
        f"{'🧪 PAPER MODE' if PAPER_MODE else '⚡ LIVE MODE'}\n\n"
        f"Strategy: BOS + CHoCH + Order Blocks\n"
        f"+ Liquidity Zones + FVG + Patterns + RSI\n\n"
        f"Capital: ${CAPITAL} | Risk/trade: ${RISK_AMOUNT:.2f}\n"
        f"Min Confidence: {MIN_CONFIDENCE}%\n"
        f"Max Signals: {quota_line}\n"
        f"Timeframes: 5M + 15M\n\n"
        f"Scanning every 5 minutes ⚔️"
    )

    while True:
        try:
            now = datetime.now(IST)
            signals_sent = prune_state(signals_sent)
            used      = len(signals_sent)
            remaining = max(0, MAX_TRADES_PER_DAY - used) if QUOTA_ENABLED else None
            sent_txt  = f"{used}/{MAX_TRADES_PER_DAY}" if QUOTA_ENABLED else f"{used} (no cap)"

            session = get_session()
            print(f"\n[{now.strftime('%H:%M')} IST] {session} | "
                  f"Signals (last {SIGNAL_WINDOW_HOURS:g}h): {sent_txt}")

            # The quota limits alerts, never the scan — setups we cannot send
            # are still logged so nothing is silently missed.
            if market_closed():
                if scan_event.is_set():
                    scan_event.clear()
                    print("Manual scan ignored: market closed.")
                    send_telegram(
                        "⚠️ <b>Market closed</b>\n"
                        "Gold trades Sun 18:00 – Fri 17:00 ET "
                        "(daily break 17:00–18:00 ET)."
                    )
                print("Market closed. Sleeping 15min...")
                scan_event.wait(timeout=900)
                continue

            print("Fetching candles...")
            candles_5m = get_candles("5min", 100)
            time.sleep(3)
            candles_15m = get_candles("15min", 100)

            if not candles_5m or not candles_15m:
                print("Fetch failed. Retry in 2min...")
                time.sleep(120)
                continue

            journal = evaluate_journal(journal, candles_5m)
            stats   = journal_stats(journal)
            db_save_bars(candles_5m, "5min")
            db_save_bars(candles_15m, "15min")
            journal_cache, stats_cache = journal, stats

            sig = generate_signal(candles_5m, candles_15m)

            if sig:
                now_ts = time.time()
                wait = same_direction_wait(signals_sent, sig["signal"], now_ts)
                if alerts_paused:
                    print(f"⏸ Paused — {sig['signal']} @ {sig['price']} not sent.")
                elif wait > 0:
                    print(f"{sig['signal']} within {SAME_DIRECTION_COOLDOWN // 60}min "
                          f"of the last {sig['signal']} — skipping ({wait}s left).")
                elif QUOTA_ENABLED and remaining <= 0:
                    print(f"⏭ Skipped (quota {used}/{MAX_TRADES_PER_DAY}): "
                          f"{sig['signal']} @ {sig['price']} | "
                          f"conf {sig['confidence']}% | next slot in "
                          f"{format_wait(signals_sent, now_ts)}")
                else:
                    send_signal(sig, candles_5m)
                    ai_started = time.time()
                    opinion = ai_second_opinion(sig, candles_5m, candles_15m, stats)
                    journal = record_signal(journal, sig, opinion, candles_5m[-1]["time"])
                    if opinion:
                        send_ai_opinion(sig, opinion, stats)
                        db_save_ai_review(journal[-1], OPENROUTER_MODEL, opinion,
                                          int((time.time() - ai_started) * 1000))
                    signals_sent.append({"ts": now_ts, "direction": sig["signal"]})
                    save_state(signals_sent)
                    if QUOTA_ENABLED:
                        remaining -= 1
                        if remaining == 0:
                            send_telegram(
                                f"🔴 <b>Signal Quota Reached</b>\n"
                                f"{MAX_TRADES_PER_DAY} signals in the last "
                                f"{SIGNAL_WINDOW_HOURS:g}h.\n"
                                f"Still scanning — next alert in "
                                f"{format_wait(signals_sent)}."
                            )
            else:
                print("No signal. Waiting...")

            # Clock-sync: wake exactly at next 5M candle close, or early on /scan
            ts         = time.time()
            sleep_time = (300 - (ts % 300)) + 3
            print(f"Sleeping {int(sleep_time)}s until next candle close...")
            if scan_event.wait(timeout=sleep_time):
                scan_event.clear()
                print("Manual scan — skipping the wait.")

        except KeyboardInterrupt:
            send_telegram("🔴 <b>Bot stopped.</b>")
            break
        except Exception as e:
            print(f"Error: {e}")
            time.sleep(120)

if __name__ == "__main__":
    main()
