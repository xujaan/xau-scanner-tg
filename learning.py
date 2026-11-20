#!/usr/bin/env python3
"""Bounded self-learning from live and shadow results.

This module does not rewrite the strategy. It ranks the shadow variants with
statistics, applies a minimum-sample promotion rule, and lets the AI write a
post-mortem/recommendation. Nothing is applied to live automatically.
"""

import json
import time

import bot

CATALOG = tuple(name for name, _rules in bot.SHADOW_VARIANTS)
CHAMPION = "vol_high"  # shadow proxy for the current live ATR gate

AI_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["decision", "challenger", "confidence", "reason",
                 "next_experiment", "risk_note"],
    "properties": {
        "decision": {"type": "string", "enum": ["keep", "experiment", "reduce_risk"]},
        "challenger": {"type": "string", "enum": ["none", *CATALOG]},
        "confidence": {"type": "integer", "minimum": 0, "maximum": 100},
        "reason": {"type": "string"},
        "next_experiment": {"type": "string"},
        "risk_note": {"type": "string"},
    },
}

AI_SYSTEM = (
    "You are a bounded strategy reviewer for a gold (XAU/USD) signal bot. "
    "You cannot change code, place orders, or invent parameters. Choose only "
    "from the supplied variant catalog. Weigh sample size, net E[R], drawdown, "
    "and regime bias: the shadow sample can be mostly one direction. Keep the "
    "champion unless a challenger has enough trades and a clear risk-adjusted "
    "edge. Do not claim to know future prices. Reply with JSON only."
)

_last_attempt = 0.0


def _clean(journal):
    return [r for r in journal
            if r.get("entry_bar") and r.get("outcome") in ("TP", "SL")]


def live_summary(journal):
    rows = _clean(journal)
    tp = sum(1 for r in rows if r["outcome"] == "TP")
    sl = len(rows) - tp
    net = 0.0
    for rec in rows:
        risk = abs((rec.get("entry") or 0) - (rec.get("sl") or 0))
        gross = 2.0 if rec["outcome"] == "TP" else -1.0
        net += gross - (bot.SHADOW_SPREAD / risk if risk > 0 else 0.0)
    closed = tp + sl
    return {
        "closed": closed,
        "tp": tp,
        "sl": sl,
        "wr": round(100.0 * tp / closed, 1) if closed else 0.0,
        "e_r": round((2.0 * tp - sl) / closed, 3) if closed else 0.0,
        "net_total_r": round(net, 2),
        "net_e_r": round(net / closed, 3) if closed else 0.0,
        "max_dd": round(bot.max_drawdown_r(rows), 2),
    }


def shadow_ranking(shadow):
    stats = bot.shadow_stats(shadow)
    ranking = []
    for variant, _rules in bot.SHADOW_VARIANTS:
        if variant not in stats:
            continue
        row = {"variant": variant}
        row.update(stats[variant])
        ranking.append(row)
    ranking.sort(key=lambda r: (r["net_e_r"], r["net_total_r"]), reverse=True)
    return ranking


def champion_row(ranking):
    for row in ranking:
        if row["variant"] == CHAMPION:
            return row
    return ranking[0] if ranking else None


def recommendation(ranking, bias=None):
    champ = champion_row(ranking)
    if not champ:
        return {"decision": "collect", "variant": "none",
                "reason": "Belum ada data shadow."}
    if champ["closed"] < bot.LEARNING_MIN_TRADES:
        return {
            "decision": "collect",
            "variant": champ["variant"],
            "reason": f"Champion baru {champ['closed']}/{bot.LEARNING_MIN_TRADES} trade.",
        }
    qualified = [r for r in ranking
                 if r["variant"] != champ["variant"]
                 and r["closed"] >= bot.LEARNING_MIN_TRADES]
    best = max(qualified, key=lambda r: r["net_e_r"], default=None)
    if best is None:
        return {"decision": "keep", "variant": champ["variant"],
                "reason": "Belum ada challenger dengan sampel cukup."}
    bias = bias or {}
    if (best["variant"] in ("short_only", "short_vol")
            and bias.get("long", {}).get("closed", 0) < 20):
        return {
            "decision": "keep",
            "variant": champ["variant"],
            "challenger": best["variant"],
            "reason": (f"{best['variant']} unggul, tapi sampel shadow masih "
                       f"short-heavy (LONG hanya {bias.get('long', {}).get('closed', 0)}). "
                       "Jangan promosikan filter arah dari satu regime."),
        }
    dd_limit = max(champ["max_dd"] * 1.25, champ["max_dd"] + 1.0)
    if (best["net_e_r"] >= champ["net_e_r"] + 0.05
            and best["max_dd"] <= dd_limit):
        return {
            "decision": "promote",
            "variant": best["variant"],
            "challenger": best["variant"],
            "reason": (f"{best['variant']} net E[R] {best['net_e_r']:+.3f} vs "
                       f"{champ['variant']} {champ['net_e_r']:+.3f}, "
                       f"DD {best['max_dd']:.1f}R vs {champ['max_dd']:.1f}R."),
        }
    return {
        "decision": "keep",
        "variant": champ["variant"],
        "challenger": best["variant"],
        "reason": (f"Challenger terbaik {best['variant']} belum melewati syarat; "
                   f"net E[R] {best['net_e_r']:+.3f} vs {champ['net_e_r']:+.3f}."),
    }


def direction_bias(shadow):
    rows = [r for r in shadow
            if r.get("variant") == "baseline" and r.get("outcome") in ("TP", "SL")]
    out = {}
    for direction in ("LONG", "SHORT"):
        side = [r for r in rows if r["direction"] == direction]
        tp = sum(1 for r in side if r["outcome"] == "TP")
        out[direction.lower()] = {
            "closed": len(side), "tp": tp,
            "wr": round(100.0 * tp / len(side), 1) if side else 0.0,
        }
    return out


def build_context(journal, shadow):
    ranking = shadow_ranking(shadow)
    bias = direction_bias(shadow)
    return {
        "live": live_summary(journal),
        "champion": champion_row(ranking),
        "ranking": ranking,
        "recommendation": recommendation(ranking, bias),
        "direction_bias": bias,
        "filters": {
            "min_entry_atr": bot.MIN_ENTRY_ATR,
            "max_open_positions": bot.MAX_OPEN_POSITIONS,
            "max_portfolio_heat_pct": bot.MAX_PORTFOLIO_HEAT_PCT,
            "risk_percent": bot.RISK_PERCENT,
            "min_trades_for_promotion": bot.LEARNING_MIN_TRADES,
        },
        "spread": bot.SHADOW_SPREAD,
    }


def _post_ai(context):
    body = {
        "model": bot.OPENROUTER_MODEL,
        "messages": [
            {"role": "system", "content": AI_SYSTEM},
            {"role": "user", "content": json.dumps(context)},
        ],
        "temperature": 0.2,
        "max_tokens": 1000,
        "reasoning": {"enabled": False},
        "response_format": {"type": "json_schema", "json_schema": {
            "name": "learning_review", "strict": True, "schema": AI_SCHEMA}},
    }
    try:
        response = bot.requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={"Authorization": f"Bearer {bot.OPENROUTER_API_KEY}",
                     "Content-Type": "application/json"},
            json=body, timeout=bot.AI_TIMEOUT)
        if response.status_code != 200:
            print(f"Learning AI error {response.status_code}: {response.text[:200]}")
            return None
        data = response.json()
    except Exception as exc:
        print(f"Learning AI call failed: {exc}")
        return None
    content, reason = bot._ai_message_content(data)
    if not content:
        print(f"Learning AI empty response: {reason}")
        return None
    opinion = bot._parse_ai_json(content)
    if not isinstance(opinion, dict):
        print("Learning AI returned non-JSON content.")
        return None
    decision = str(opinion.get("decision", "")).lower()
    challenger = str(opinion.get("challenger", "none"))
    if decision not in ("keep", "experiment", "reduce_risk"):
        return None
    if challenger not in ("none", *CATALOG):
        return None
    try:
        confidence = int(opinion.get("confidence"))
    except (TypeError, ValueError):
        return None
    if not 0 <= confidence <= 100:
        return None
    return {
        "ts": time.time(),
        "decision": decision,
        "challenger": challenger,
        "confidence": confidence,
        "reason": str(opinion.get("reason") or "")[:600],
        "next_experiment": str(opinion.get("next_experiment") or "")[:400],
        "risk_note": str(opinion.get("risk_note") or "")[:400],
    }


def save_ai_review(opinion):
    bot.db_run([("INSERT INTO learning_log (ts, kind, payload) VALUES (?,?,?)",
                 (opinion["ts"], "ai_review", json.dumps(opinion)))])


def last_ai_review():
    rows = bot.db_run([("SELECT ts, payload FROM learning_log "
                        "WHERE kind='ai_review' ORDER BY ts DESC LIMIT 1", ())],
                      fetch=True)
    if not rows:
        return None
    try:
        opinion = json.loads(rows[0][1])
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    opinion["ts"] = rows[0][0]
    return opinion


def run_ai_review(journal, shadow):
    if not (bot.LEARNING_ENABLED and bot.LEARNING_AI and bot.AI_ENABLED):
        return None
    context = build_context(journal, shadow)
    opinion = _post_ai(context)
    if opinion:
        save_ai_review(opinion)
    return opinion


def maybe_auto_review(journal, shadow):
    global _last_attempt
    if not (bot.LEARNING_ENABLED and bot.LEARNING_AI and bot.AI_ENABLED):
        return None
    now = time.time()
    if now - _last_attempt < 6 * 3600:
        return None
    _last_attempt = now
    last = last_ai_review()
    if last and now - float(last.get("ts") or 0) < bot.LEARNING_AI_HOURS * 3600:
        return None
    context = build_context(journal, shadow)
    if (context["live"]["closed"] < bot.LEARNING_MIN_TRADES
            and (context["champion"] or {}).get("closed", 0) < bot.LEARNING_MIN_TRADES):
        return None
    return run_ai_review(journal, shadow)


def format_ai(opinion):
    if not opinion:
        return "🧠 <b>AI review</b>\nTidak ada hasil — cek log AI."
    icons = {"keep": "🛡", "experiment": "🧪", "reduce_risk": "⚠️"}
    return (
        f"🧠 <b>AI learning review</b>\n"
        f"{icons.get(opinion['decision'], '•')} Decision: <b>{opinion['decision']}</b> "
        f"({opinion['confidence']}%)\n"
        f"Challenger: {opinion['challenger']}\n"
        f"Reason: {opinion['reason']}\n"
        f"Next: {opinion['next_experiment']}\n"
        f"Risk: {opinion['risk_note']}"
    )


def report_text(journal, shadow):
    context = build_context(journal, shadow)
    live = context["live"]
    champ = context["champion"] or {}
    rec = context["recommendation"]
    lines = [
        "🧠 <b>Self-learning</b>",
        f"Live: {live['closed']} closed | WR {live['wr']:.1f}% | "
        f"net {live['net_total_r']:+.1f}R | DD {live['max_dd']:.1f}R",
        f"Champion: <b>{champ.get('variant', 'none')}</b> — "
        f"{champ.get('closed', 0)} closed | WR {champ.get('wr', 0):.1f}% | "
        f"net {champ.get('net_total_r', 0):+.1f}R | DD {champ.get('max_dd', 0):.1f}R",
        "",
        "<b>Ranking (min sample "
        f"{bot.LEARNING_MIN_TRADES})</b>",
    ]
    for row in context["ranking"]:
        mark = "★" if row["variant"] == champ.get("variant") else "•"
        lines.append(f"{mark} {row['variant']}: {row['closed']} closed | "
                     f"WR {row['wr']:.0f}% | net {row['net_total_r']:+.1f}R | "
                     f"DD {row['max_dd']:.1f}R")
    lines.append("")
    lines.append(f"<b>Rule:</b> {rec['decision']} — {rec['reason']}")
    last = last_ai_review()
    if last:
        when = time.strftime("%d %b %H:%M", time.localtime(last["ts"]))
        lines.append(f"<b>AI ({when}):</b> {last['decision']} "
                     f"({last['confidence']}%) — {last['reason'][:220]}")
    else:
        lines.append("AI review: belum ada. Kirim /learnai untuk meminta analisa.")
    return "\n".join(lines)
