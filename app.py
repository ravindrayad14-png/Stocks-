"""One-click multi-agent Indian stock-analysis dashboard.

python app.py, open the URL, click Start. See README.md for setup.
Analysis only — no orders are ever placed. Not investment advice.
"""
import json
import os
import sqlite3
import threading
import time
from datetime import datetime

import requests

import envconfig
import data_sources
import scoring
import llm

from flask import Flask, jsonify, request, send_from_directory

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "audit.db")

app = Flask(__name__)
envconfig.load_env()

AGENT_DEFS = [
    {"id": "scout", "name": "Scout", "role": "screens the stock universe for movers",
     "stat1_label": "Scanned", "stat2_label": "Shortlisted"},
    {"id": "technician", "name": "Technician", "role": "reads price action, RVOL & trend",
     "stat1_label": "Analyzed", "stat2_label": "Avg RVOL"},
    {"id": "fundamentalist", "name": "Fundamentalist", "role": "weighs valuation & analyst targets",
     "stat1_label": "Covered", "stat2_label": "Avg upside"},
    {"id": "newsdesk", "name": "Newsdesk", "role": "pulls live news & scores sentiment",
     "stat1_label": "Headlines", "stat2_label": "Net tone"},
    {"id": "bull", "name": "Bull", "role": "argues the case to buy",
     "stat1_label": "Cases", "stat2_label": "Avg score"},
    {"id": "bear", "name": "Bear", "role": "argues the case against",
     "stat1_label": "Cases", "stat2_label": "Avg score"},
    {"id": "judge", "name": "Judge", "role": "weighs the debate, issues verdict + confidence",
     "stat1_label": "Verdicts", "stat2_label": "Buy"},
    {"id": "messenger", "name": "Messenger", "role": "sends signals to Telegram",
     "stat1_label": "Sent", "stat2_label": "Engine"},
]

STATE_LOCK = threading.Lock()


def fresh_state():
    return {
        "status": "idle",  # idle | running | done
        "mode": "demo",
        "brand": envconfig.get("BRAND", "AgentDesk"),
        "agents": {
            a["id"]: {"status": "offline", "stat1": 0, "stat2": "—"} for a in AGENT_DEFS
        },
        "kpi": {"universe": 0, "in_debate": 0, "buy_signals": 0, "top_pick": None},
        "verdicts": [],
        "engine": None,
        "data_pulled_at": None,
        "run_id": None,
        "error": None,
    }


STATE = fresh_state()
# init_db() is called near the bottom of this file, after it's defined, so
# both `python app.py` and a production server (gunicorn) initialize the DB.


def set_agent(agent_id, status=None, stat1=None, stat2=None):
    with STATE_LOCK:
        a = STATE["agents"][agent_id]
        if status is not None:
            a["status"] = status
        if stat1 is not None:
            a["stat1"] = stat1
        if stat2 is not None:
            a["stat2"] = stat2


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at TEXT, mode TEXT, engine TEXT,
            universe_count INTEGER, buy_count INTEGER
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS verdicts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER, symbol TEXT, cap_segment TEXT, verdict TEXT,
            confidence INTEGER, winner TEXT, rationale TEXT, key_catalyst TEXT,
            live_price REAL, day_change_pct REAL, engine TEXT, created_at TEXT
        )
    """)
    conn.commit()
    conn.close()


def db_start_run(mode):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute(
        "INSERT INTO runs (started_at, mode, engine, universe_count, buy_count) VALUES (?, ?, ?, 0, 0)",
        (datetime.utcnow().isoformat(), mode, None),
    )
    run_id = cur.lastrowid
    conn.commit()
    conn.close()
    return run_id


def db_log_verdict(run_id, bundle, result):
    v = result["verdict"]
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """INSERT INTO verdicts
           (run_id, symbol, cap_segment, verdict, confidence, winner, rationale,
            key_catalyst, live_price, day_change_pct, engine, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (run_id, bundle["symbol"], bundle["cap_segment"], v["verdict"], v["confidence"],
         v["winner"], v["rationale"], v["key_catalyst"],
         bundle["price"].get("live"), bundle["price"].get("day_change_pct"),
         result.get("engine"), datetime.utcnow().isoformat()),
    )
    conn.commit()
    conn.close()


def db_finish_run(run_id, engine, universe_count, buy_count):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "UPDATE runs SET engine = ?, universe_count = ?, buy_count = ? WHERE id = ?",
        (engine, universe_count, buy_count, run_id),
    )
    conn.commit()
    conn.close()


# --------------------------------------------------------------------------
# Telegram
# --------------------------------------------------------------------------

def telegram_send(text):
    token = envconfig.get("TELEGRAM_BOT_TOKEN")
    chat_id = envconfig.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return False, "Telegram not configured"
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        resp = requests.post(url, json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"}, timeout=15)
        ok = resp.status_code == 200
        return ok, ("sent" if ok else f"http {resp.status_code}")
    except Exception as e:
        return False, "send failed"  # never leak token/details that might embed it


def format_buy_message(bundle, verdict):
    price = bundle["price"].get("live")
    change = bundle["price"].get("day_change_pct")
    price_s = f"₹{price}" if price is not None else "data unavailable"
    change_s = f"{change}%" if change is not None else "data unavailable"
    return (
        f"🟢 <b>BUY SIGNAL — {bundle['symbol']}</b> ({bundle['cap_segment'].title()} cap)\n\n"
        f"Verdict: BUY | Confidence: {verdict['confidence']}/10\n"
        f"Winner: {verdict['winner']}\n"
        f"Why: {verdict['rationale']}\n"
        f"Key catalyst: {verdict['key_catalyst']}\n"
        f"Live price: {price_s} | Day change: {change_s}\n\n"
        f"— Analysis only. No trade was placed. Not investment advice."
    )


def format_summary_message(fired):
    if not fired:
        return "📋 <b>Daily summary</b>\n\nNo BUY signals fired this run.\n\n— Analysis only. Not investment advice."
    lines = [f"📋 <b>Daily summary — {len(fired)} BUY signal(s)</b>", ""]
    for b, v in fired:
        lines.append(f"• {b['symbol']} — confidence {v['confidence']}/10 — {v['key_catalyst']}")
    lines.append("")
    lines.append("— Analysis only. No trade was placed. Not investment advice.")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Pipeline
# --------------------------------------------------------------------------

def run_cycle(mode):
    delay = envconfig.get_float("AGENT_DELAY", 0.6)
    threshold = envconfig.get_int("CONFIDENCE_THRESHOLD", 7)
    shortlist_n = envconfig.get_int("SHORTLIST_PER_BUCKET", 4)

    with STATE_LOCK:
        STATE.update(fresh_state())
        STATE["status"] = "running"
        STATE["mode"] = mode

    run_id = db_start_run(mode)
    STATE["run_id"] = run_id

    try:
        # --- Scout: screen the universe ---
        set_agent("scout", status="working")
        universe = data_sources.load_universe()
        total_scanned = sum(len(v) for v in universe.values()) if mode == "live" else None
        buckets = data_sources.get_universe_buckets(mode, shortlist_n)
        shortlisted = [b for seg in buckets.values() for b in seg]
        scanned_count = total_scanned if total_scanned is not None else len(shortlisted)
        set_agent("scout", status="done", stat1=scanned_count, stat2=len(shortlisted))
        with STATE_LOCK:
            STATE["kpi"]["universe"] = scanned_count
            STATE["kpi"]["in_debate"] = len(shortlisted)
            STATE["data_pulled_at"] = data_sources.now_ist_str()
        time.sleep(delay)

        if not shortlisted:
            raise RuntimeError("no stocks shortlisted")

        # --- Technician / Fundamentalist / Newsdesk: pass over evidence (informational pass) ---
        for agent_id, extractor in (
            ("technician", lambda b: b["technicals"].get("rvol")),
            ("fundamentalist", lambda b: b["analyst"].get("upside_pct")),
            ("newsdesk", lambda b: b["news"].get("total")),
        ):
            set_agent(agent_id, status="working")
            vals = [extractor(b) for b in shortlisted if extractor(b) is not None]
            avg = round(sum(vals) / len(vals), 2) if vals else "—"
            set_agent(agent_id, status="done", stat1=len(shortlisted), stat2=avg)
            time.sleep(delay * 0.5)

        # --- Bull / Bear / Judge: debate + verdict, stock by stock ---
        set_agent("bull", status="working")
        set_agent("bear", status="working")
        set_agent("judge", status="working")

        provider = llm.detect_provider()
        engine_label = f"llm:{provider}" if provider else "deterministic"

        bull_scores, bear_scores = [], []
        buy_count = 0
        verdicts_out = []
        fired_for_telegram = []

        for bundle in shortlisted:
            result = llm.evaluate(bundle)
            v = result["verdict"]
            bull_scores.append(v["bull_score"])
            bear_scores.append(v["bear_score"])
            if v["verdict"] == "BUY":
                buy_count += 1

            row = {
                "symbol": bundle["symbol"],
                "name": bundle.get("name", bundle["symbol"]),
                "cap_segment": bundle["cap_segment"],
                "verdict": v["verdict"],
                "confidence": v["confidence"],
                "why": v["rationale"],
                "key_catalyst": v["key_catalyst"],
                "live_price": bundle["price"].get("live"),
                "day_change_pct": bundle["price"].get("day_change_pct"),
                "engine": result.get("engine"),
            }
            verdicts_out.append(row)
            db_log_verdict(run_id, bundle, result)

            if v["verdict"] == "BUY" and v["confidence"] >= threshold:
                fired_for_telegram.append((bundle, v))

            with STATE_LOCK:
                STATE["verdicts"] = list(verdicts_out)
                STATE["kpi"]["buy_signals"] = buy_count
                top = max(verdicts_out, key=lambda r: r["confidence"]) if verdicts_out else None
                if top:
                    STATE["kpi"]["top_pick"] = f"{top['symbol']} ({top['confidence']}/10)"
            time.sleep(delay * 0.3)

        avg_bull = round(sum(bull_scores) / len(bull_scores), 1) if bull_scores else "—"
        avg_bear = round(sum(bear_scores) / len(bear_scores), 1) if bear_scores else "—"
        set_agent("bull", status="done", stat1=len(shortlisted), stat2=avg_bull)
        set_agent("bear", status="done", stat1=len(shortlisted), stat2=avg_bear)
        set_agent("judge", status="done", stat1=len(shortlisted), stat2=buy_count)
        time.sleep(delay)

        # --- Messenger: Telegram delivery ---
        set_agent("messenger", status="working")
        sent = 0
        for bundle, v in fired_for_telegram:
            ok, _ = telegram_send(format_buy_message(bundle, v))
            if ok:
                sent += 1
        telegram_send(format_summary_message(fired_for_telegram))
        set_agent("messenger", status="done", stat1=sent, stat2=engine_label)

        db_finish_run(run_id, engine_label, len(shortlisted), buy_count)

        with STATE_LOCK:
            STATE["status"] = "done"
            STATE["engine"] = engine_label

    except Exception as e:
        with STATE_LOCK:
            STATE["status"] = "done"
            STATE["error"] = str(e)


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------

@app.route("/")
def index():
    return send_from_directory(BASE_DIR, "dashboard.html")


@app.route("/start", methods=["POST"])
def start():
    with STATE_LOCK:
        if STATE["status"] == "running":
            return jsonify({"ok": False, "error": "already running"}), 409
    payload = request.get_json(silent=True) or {}
    mode = payload.get("mode", "demo")
    if mode not in ("demo", "live"):
        mode = "demo"
    thread = threading.Thread(target=run_cycle, args=(mode,), daemon=True)
    thread.start()
    return jsonify({"ok": True, "mode": mode})


@app.route("/status")
def status():
    with STATE_LOCK:
        payload = {
            "status": STATE["status"],
            "mode": STATE["mode"],
            "brand": STATE["brand"],
            "agents": {aid: dict(a) for aid, a in STATE["agents"].items()},
            "agent_meta": {a["id"]: a for a in AGENT_DEFS},
            "kpi": dict(STATE["kpi"]),
            "verdicts": list(reversed(STATE["verdicts"])),
            "engine": STATE["engine"],
            "data_pulled_at": STATE["data_pulled_at"],
            "error": STATE["error"],
        }
    return jsonify(payload)


@app.route("/config")
def config():
    return jsonify({
        "brand": envconfig.get("BRAND", "AgentDesk"),
        "confidence_threshold": envconfig.get_int("CONFIDENCE_THRESHOLD", 7),
        "shortlist_per_bucket": envconfig.get_int("SHORTLIST_PER_BUCKET", 4),
        "telegram_configured": bool(envconfig.get("TELEGRAM_BOT_TOKEN")) and bool(envconfig.get("TELEGRAM_CHAT_ID")),
        "llm_provider": llm.detect_provider(),
        "agents": AGENT_DEFS,
    })


init_db()  # runs on import — so `gunicorn app:app` (cloud) initializes too

if __name__ == "__main__":
    port = envconfig.get_int("PORT", 5000)
    print(f"Dashboard running at http://127.0.0.1:{port}")
    print("On your local network, open this from your iPad/phone's browser:")
    try:
        import socket
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
        s.close()
        print(f"  http://{local_ip}:{port}")
    except Exception:
        print("  http://<this-computer's-local-IP>:{port}  (find it in your WiFi settings)".format(port=port))
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
