"""
ORB + 4H PO3 notifier: TradingView alert -> Telegram message. Notification only, no trading.

Endpoints
  POST /tradingview   TradingView webhook (JSON from orb_po3_notifier.pine)
  POST /telegram      Telegram bot webhook (your commands)
  GET  /              health check

Telegram commands (only from your chat)
  /status   on/paused, notification window, alerts today
  /pause    stop notifications       /resume   start again
  /test     send a sample notification
  /id       show this chat's ID (for first-time setup)
"""
import json
import logging
import os
import threading
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

import requests
from flask import Flask, abort, request

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


def env(name, default=None):
    v = os.environ.get(name, default)
    return v.strip() if isinstance(v, str) else v


def env_bool(name, default):
    return env(name, str(default)).lower() in ("1", "true", "yes", "on")


# ── Config ───────────────────────────────────────────────────────────────────
TV_SECRET = env("TV_SECRET", "change-me")
TELEGRAM_BOT_TOKEN = env("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = env("TELEGRAM_CHAT_ID", "")
TELEGRAM_WEBHOOK_SECRET = env("TELEGRAM_WEBHOOK_SECRET", "")
PUBLIC_URL = env("PUBLIC_URL", "").rstrip("/")

ALERT_START = env("ALERT_START", "09:30")      # New York time
ALERT_END = env("ALERT_END", "11:30")
WEEKDAYS_ONLY = env_bool("WEEKDAYS_ONLY", True)
POINT_VALUE = float(env("POINT_VALUE", "2"))   # MNQ = $2/point per contract (for the $ line)
STATE_FILE = env("STATE_FILE", "state.json")
TZ = ZoneInfo("America/New_York")
TG_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("notifier")
app = Flask(__name__)

lock = threading.Lock()
state = {"paused": False, "recent": [], "count": {}}


def load_state():
    try:
        with open(STATE_FILE) as f:
            saved = json.load(f)
        for k in state:
            if k in saved:
                state[k] = saved[k]
    except (FileNotFoundError, json.JSONDecodeError):
        pass


def save_state():
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f)
    os.replace(tmp, STATE_FILE)


load_state()


def hhmm(s):
    h, m = s.split(":")
    return dtime(int(h), int(m))


def now_ny():
    return datetime.now(TZ)


def in_window(now=None):
    now = now or now_ny()
    if WEEKDAYS_ONLY and now.weekday() >= 5:
        return False
    return hhmm(ALERT_START) <= now.time() < hhmm(ALERT_END)


def today():
    return now_ny().strftime("%Y-%m-%d")


# ── Telegram ─────────────────────────────────────────────────────────────────
def tg(method, **params):
    if not TELEGRAM_BOT_TOKEN:
        log.info("[Telegram not configured] %s %s", method, params.get("text", ""))
        return None
    try:
        data = requests.post(f"{TG_API}/{method}", json=params, timeout=10).json()
        if not data.get("ok"):
            log.warning("Telegram %s failed: %s", method, data)
        return data.get("result")
    except Exception:
        log.exception("Telegram %s failed", method)
        return None


def send(text, chat_id=None):
    return tg("sendMessage", chat_id=chat_id or TELEGRAM_CHAT_ID, text=text)


def register_webhook():
    if not (TELEGRAM_BOT_TOKEN and PUBLIC_URL):
        log.warning("PUBLIC_URL or TELEGRAM_BOT_TOKEN missing: Telegram webhook not registered")
        return
    params = {"url": f"{PUBLIC_URL}/telegram", "allowed_updates": ["message"]}
    if TELEGRAM_WEBHOOK_SECRET:
        params["secret_token"] = TELEGRAM_WEBHOOK_SECRET
    tg("setWebhook", **params)
    tg("setMyCommands", commands=[
        {"command": "status", "description": "On/paused, window, alerts today"},
        {"command": "pause", "description": "Stop notifications"},
        {"command": "resume", "description": "Start notifications again"},
        {"command": "test", "description": "Send a sample notification"},
    ])
    log.info("Telegram webhook set to %s/telegram", PUBLIC_URL)


# ── Message formatting ───────────────────────────────────────────────────────
STAGE = {"forming": "⏳ FORMING", "confirmed": "✅ CONFIRMED", "exit": "🚪 EXIT"}


def fmt(x):
    return f"{x:,.2f}"


def build_message(sig):
    arrow = "🟢" if sig["side"] == "long" else "🔴"
    lines = [f"{STAGE[sig['stage']]} · {sig['tool']}"]
    kind = f" · {sig['kind']}" if sig["kind"] and sig["stage"] != "exit" else ""
    lines.append(f"{arrow} {sig['symbol']} {sig['side'].upper()}{kind}")
    lines.append(sig["setup"])
    if sig["stage"] == "exit":
        lines.append(f"{sig['kind'] or sig['note']} @ {fmt(sig['entry'])}")
    else:
        risk = abs(sig["entry"] - sig["stop"])
        reward = abs(sig["target"] - sig["entry"])
        rr = f"{reward / risk:.1f}R" if risk > 0 else "—"
        lines.append(f"Entry ~{fmt(sig['entry'])}")
        lines.append(f"Stop {fmt(sig['stop'])}  ({risk:.2f} pts · ${risk * POINT_VALUE:,.0f} per MNQ)")
        lines.append(f"Target {fmt(sig['target'])}  ({rr})")
        if sig["note"]:
            lines.append(f"ℹ️ {sig['note']}")
    chart = f" · {sig['tf']}m chart" if sig["tf"].isdigit() else ""
    lines.append(f"🕒 {now_ny().strftime('%H:%M')} ET{chart}")
    return "\n".join(lines)


# ── Routes ───────────────────────────────────────────────────────────────────
@app.get("/")
def health():
    return {"ok": True, "mode": "notification only", "paused": state["paused"],
            "window": f"{ALERT_START}-{ALERT_END} ET", "in_window": in_window()}


@app.post("/tradingview")
def tradingview():
    try:
        data = json.loads(request.get_data(as_text=True))
    except json.JSONDecodeError:
        abort(400, "not JSON")
    if data.get("secret") != TV_SECRET:
        abort(403)
    try:
        sig = {
            "stage": str(data["stage"]).lower(), "tool": data.get("tool", ""), "setup": data.get("setup", ""),
            "side": str(data["side"]).lower(), "kind": data.get("kind", ""), "note": data.get("note", ""),
            "symbol": data.get("symbol", ""), "tf": str(data.get("tf", "")),
            "entry": float(data["entry"]), "stop": float(data["stop"]), "target": float(data["target"]),
            "bar_time": data.get("bar_time"),
        }
    except (KeyError, ValueError, TypeError):
        abort(400, "missing fields")
    if sig["stage"] not in STAGE or sig["side"] not in ("long", "short"):
        abort(400, "bad stage/side")

    with lock:
        key = f"{sig['stage']}|{sig['tool']}|{sig['side']}|{sig['setup']}|{sig['bar_time']}"
        if key in state["recent"]:
            return {"ok": True, "duplicate": True}
        state["recent"] = (state["recent"] + [key])[-100:]
        if state["paused"]:
            save_state()
            return {"ok": True, "paused": True}
        if not in_window():
            save_state()
            log.info("Outside %s-%s ET, not sent: %s", ALERT_START, ALERT_END, sig["setup"])
            return {"ok": True, "outside_window": True}
        state["count"] = {today(): state["count"].get(today(), 0) + 1}
        save_state()

    send(build_message(sig))
    return {"ok": True, "sent": True}


@app.post("/telegram")
def telegram():
    if TELEGRAM_WEBHOOK_SECRET and request.headers.get("X-Telegram-Bot-Api-Secret-Token") != TELEGRAM_WEBHOOK_SECRET:
        abort(403)
    msg = (request.get_json(silent=True) or {}).get("message") or {}
    chat_id = str(msg.get("chat", {}).get("id", ""))
    text = (msg.get("text") or "").strip()
    if not chat_id or not text:
        return {"ok": True}
    if chat_id != TELEGRAM_CHAT_ID:
        if text.lower().startswith(("/id", "/start")):
            send(f"Your chat ID is {chat_id}\nPut it in TELEGRAM_CHAT_ID and restart the bot.", chat_id=chat_id)
        return {"ok": True}
    send(handle_command(text))
    return {"ok": True}


def handle_command(text):
    t = text.strip().lower().lstrip("/").split("@")[0]
    if t in ("pause", "stop"):
        with lock:
            state["paused"] = True
            save_state()
        return "⏸ Notifications paused. /resume to turn them back on."
    if t == "resume":
        with lock:
            state["paused"] = False
            save_state()
        return "▶️ Notifications on."
    if t == "test":
        return build_message({"stage": "confirmed", "tool": "ORB", "setup": "TEST · ORB breakout above 21,050.00",
                              "side": "long", "kind": "Continuation", "note": "This is only a test",
                              "symbol": "MNQ1!", "tf": "1", "entry": 21052.25, "stop": 21031.50,
                              "target": 21093.75, "bar_time": 0})
    window = f"{ALERT_START}–{ALERT_END} ET" + (" Mon–Fri" if WEEKDAYS_ONLY else "")
    return (f"{'⏸ Paused' if state['paused'] else '▶️ On'} · notification only\n"
            f"Window: {window} ({'open now' if in_window() else 'closed now'})\n"
            f"Notifications today: {state['count'].get(today(), 0)}\n\n"
            "Commands: /status /pause /resume /test")


register_webhook()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(env("PORT", "5000")))
