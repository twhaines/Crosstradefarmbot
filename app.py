"""
Trade alert bot: TradingView alert -> Telegram message with YES/NO buttons -> CrossTrade places the order.

Endpoints
  POST /tradingview   TradingView webhook (JSON from signal_scanner.pine)
  POST /telegram      Telegram bot webhook (button taps + your messages)
  GET  /              health check

Telegram commands (only from your chat)
  Tap ✅ Take it / ❌ Skip on a setup, or type: YES 7 / NO 7
  /pause    stop sending new setups        /resume   start again
  /flat     close the MNQ position + cancel its orders now
  /status   mode, trades today, waiting setups
  /id       show this chat's ID (for first-time setup)
"""
import json
import logging
import os
import re
import threading
import time
from datetime import datetime
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
TELEGRAM_CHAT_ID = env("TELEGRAM_CHAT_ID", "")         # your chat ID (send /id to the bot to get it)
TELEGRAM_WEBHOOK_SECRET = env("TELEGRAM_WEBHOOK_SECRET", "")  # letters/numbers/_/- only
PUBLIC_URL = env("PUBLIC_URL", "").rstrip("/")         # e.g. https://your-bot.up.railway.app

CROSSTRADE_WEBHOOK_URL = env("CROSSTRADE_WEBHOOK_URL", "")
CROSSTRADE_KEY = env("CROSSTRADE_KEY", "")
NT_ACCOUNT = env("NT_ACCOUNT", "Sim101")               # start on the sim account!
NT_INSTRUMENT = env("NT_INSTRUMENT", "MNQ 12-26")      # update on contract roll
ENTRY_COMMAND = env("ENTRY_COMMAND", "flatplace")      # flatplace = close any open MNQ position first
QTY = int(env("QTY", "1"))
POINT_VALUE = float(env("POINT_VALUE", "2"))           # MNQ = $2 per point

APPROVAL_MINUTES = float(env("APPROVAL_MINUTES", "3"))
MAX_TRADES_PER_DAY = int(env("MAX_TRADES_PER_DAY", "3"))
SEND_FORMING = env_bool("SEND_FORMING", True)
DRY_RUN = env_bool("DRY_RUN", True)                    # True = never send orders
STATE_FILE = env("STATE_FILE", "state.json")
TZ = ZoneInfo("America/New_York")
TG_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("bot")

app = Flask(__name__)

# ── State (in memory, saved to a small JSON file) ────────────────────────────
lock = threading.Lock()
state = {"next_id": 1, "pending": {}, "trades": {}, "paused": False, "recent": []}


def load_state():
    try:
        with open(STATE_FILE) as f:
            state.update(json.load(f))
    except (FileNotFoundError, json.JSONDecodeError):
        pass


def save_state():
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f)
    os.replace(tmp, STATE_FILE)


def today():
    return datetime.now(TZ).strftime("%Y-%m-%d")


def trades_today():
    return state["trades"].get(today(), 0)


def drop_expired():
    """Remove expired setups; returns them so their buttons can be cleared."""
    now = time.time()
    gone = {k: s for k, s in state["pending"].items() if s["expires_at"] < now}
    for k in gone:
        state["pending"].pop(k)
    return gone


load_state()


# ── Telegram ─────────────────────────────────────────────────────────────────
def tg(method, **params):
    if not TELEGRAM_BOT_TOKEN:
        log.info("[Telegram not configured] %s %s", method, params.get("text", ""))
        return None
    try:
        r = requests.post(f"{TG_API}/{method}", json=params, timeout=10)
        data = r.json()
        if not data.get("ok"):
            log.warning("Telegram %s failed: %s", method, data)
        return data.get("result")
    except Exception:
        log.exception("Telegram %s failed", method)
        return None


def send(text, chat_id=None, buttons=None):
    params = {"chat_id": chat_id or TELEGRAM_CHAT_ID, "text": text}
    if buttons:
        params["reply_markup"] = {"inline_keyboard": buttons}
    msg = tg("sendMessage", **params)
    return msg.get("message_id") if isinstance(msg, dict) else None


def close_message(sig, note):
    """Edit a setup message: remove its buttons and append the outcome."""
    if sig.get("msg_id"):
        tg("editMessageText", chat_id=TELEGRAM_CHAT_ID, message_id=sig["msg_id"],
           text=f"{sig.get('msg_text', '')}\n\n{note}")


def register_webhook():
    if not (TELEGRAM_BOT_TOKEN and PUBLIC_URL):
        return
    params = {"url": f"{PUBLIC_URL}/telegram", "allowed_updates": ["message", "callback_query"]}
    if TELEGRAM_WEBHOOK_SECRET:
        params["secret_token"] = TELEGRAM_WEBHOOK_SECRET
    tg("setWebhook", **params)
    tg("setMyCommands", commands=[
        {"command": "status", "description": "Mode, trades today, waiting setups"},
        {"command": "flat", "description": "Close the MNQ position now"},
        {"command": "pause", "description": "Stop sending setups"},
        {"command": "resume", "description": "Start sending setups again"},
    ])
    log.info("Telegram webhook set to %s/telegram", PUBLIC_URL)


# ── CrossTrade ───────────────────────────────────────────────────────────────
def crosstrade(payload):
    """Send one command to CrossTrade. Returns (ok, detail)."""
    full = {"key": CROSSTRADE_KEY, **payload}
    if DRY_RUN:
        log.info("[DRY RUN] would send to CrossTrade: %s", {**full, "key": "***"})
        return True, "dry run, no order sent"
    if not CROSSTRADE_WEBHOOK_URL or not CROSSTRADE_KEY:
        return False, "CrossTrade URL/key not set"
    try:
        r = requests.post(CROSSTRADE_WEBHOOK_URL, data=json.dumps(full),
                          headers={"Content-Type": "application/json"}, timeout=10)
        log.info("CrossTrade %s: %s", r.status_code, r.text[:300])
        return r.ok, f"HTTP {r.status_code}"
    except requests.RequestException as e:
        log.exception("CrossTrade request failed")
        return False, str(e)


def order_for(sig):
    tick = sig["tick"]
    stop_ticks = max(1, round(abs(sig["entry"] - sig["stop"]) / tick))
    tgt_ticks = max(1, round(abs(sig["target"] - sig["entry"]) / tick))
    return {
        "command": ENTRY_COMMAND,
        "account": NT_ACCOUNT,
        "instrument": NT_INSTRUMENT,
        "action": "buy" if sig["side"] == "long" else "sell",
        "qty": QTY,
        "order_type": "market",
        "tif": "day",
        # Measured from your actual fill, so a few seconds of delay doesn't change your risk
        "stop_loss": f"{stop_ticks}ticks",
        "take_profit": f"{tgt_ticks}ticks",
    }


# ── Message formatting ───────────────────────────────────────────────────────
def describe(sig):
    risk_pts = abs(sig["entry"] - sig["stop"])
    reward_pts = abs(sig["target"] - sig["entry"])
    return (f"Entry ~{sig['entry']:.2f}\n"
            f"Stop {sig['stop']:.2f} (-${risk_pts * POINT_VALUE * QTY:,.2f})\n"
            f"Target {sig['target']:.2f} (+${reward_pts * POINT_VALUE * QTY:,.2f})")


def headline(sig):
    arrow = "🟢" if sig["side"] == "long" else "🔴"
    return f"{arrow} {sig['symbol']} {sig['side'].upper()} - {sig['setup']}"


# ── Routes ───────────────────────────────────────────────────────────────────
@app.get("/")
def health():
    return {"ok": True, "dry_run": DRY_RUN, "account": NT_ACCOUNT, "paused": state["paused"]}


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
            "stage": data["stage"], "setup": data["setup"], "side": data["side"],
            "symbol": data.get("symbol", "MNQ"), "tf": data.get("tf", ""),
            "entry": float(data["entry"]), "stop": float(data["stop"]),
            "target": float(data["target"]), "tick": float(data.get("tick", 0.25)),
            "bar_time": data.get("bar_time"),
        }
    except (KeyError, ValueError, TypeError):
        abort(400, "missing fields")
    if sig["side"] not in ("long", "short"):
        abort(400, "bad side")

    with lock:
        key = f"{sig['stage']}|{sig['setup']}|{sig['side']}|{sig['bar_time']}"
        if key in state["recent"]:  # TradingView occasionally retries
            return {"ok": True, "duplicate": True}
        state["recent"] = (state["recent"] + [key])[-50:]

        if sig["stage"] == "forming":
            save_state()
            if SEND_FORMING and not state["paused"]:
                send(f"⏳ FORMING\n{headline(sig)}\n{describe(sig)}\nNo action yet. Wait for confirmation.")
            return {"ok": True}

        expired = drop_expired()
        if state["paused"]:
            save_state()
            return {"ok": True, "paused": True}
        if trades_today() >= MAX_TRADES_PER_DAY:
            save_state()
            send(f"{headline(sig)} confirmed, skipped: daily limit of {MAX_TRADES_PER_DAY} trades reached.")
            return {"ok": True, "limit": True}

        sid = str(state["next_id"])
        state["next_id"] += 1
        sig["expires_at"] = time.time() + APPROVAL_MINUTES * 60
        state["pending"][sid] = sig
        save_state()

    for s in expired.values():
        close_message(s, "⌛ Expired, no order sent.")

    mode = "DRY RUN" if DRY_RUN else NT_ACCOUNT
    text = (f"✅ CONFIRMED #{sid}\n{headline(sig)}\n{describe(sig)}\n"
            f"Qty {QTY} | {mode} | expires in {APPROVAL_MINUTES:g} min")
    msg_id = send(text, buttons=[[{"text": "✅ Take it", "callback_data": f"yes:{sid}"},
                                  {"text": "❌ Skip", "callback_data": f"no:{sid}"}]])
    with lock:
        if sid in state["pending"]:
            state["pending"][sid].update(msg_id=msg_id, msg_text=text)
            save_state()
    return {"ok": True, "id": sid}


@app.post("/telegram")
def telegram():
    if TELEGRAM_WEBHOOK_SECRET and request.headers.get("X-Telegram-Bot-Api-Secret-Token") != TELEGRAM_WEBHOOK_SECRET:
        abort(403)
    update = request.get_json(silent=True) or {}

    # Button tap
    cq = update.get("callback_query")
    if cq:
        chat_id = str(cq.get("message", {}).get("chat", {}).get("id", ""))
        if chat_id != TELEGRAM_CHAT_ID:
            tg("answerCallbackQuery", callback_query_id=cq["id"], text="Not authorized")
            return {"ok": True}
        action, _, sid = cq.get("data", "").partition(":")
        reply = handle_decision(action == "yes", sid)
        tg("answerCallbackQuery", callback_query_id=cq["id"], text=reply[:190])
        return {"ok": True}

    # Typed message
    msg = update.get("message") or {}
    chat_id = str(msg.get("chat", {}).get("id", ""))
    text = (msg.get("text") or "").strip()
    if not chat_id or not text:
        return {"ok": True}
    if text.lower().startswith(("/id", "/start")) and chat_id != TELEGRAM_CHAT_ID:
        send(f"Your chat ID is {chat_id}\nPut it in TELEGRAM_CHAT_ID and restart the bot.", chat_id=chat_id)
        return {"ok": True}
    if chat_id != TELEGRAM_CHAT_ID:
        return {"ok": True}  # ignore strangers silently
    send(handle_command(text))
    return {"ok": True}


def handle_command(text):
    t = text.strip().upper().lstrip("/").split("@")[0]
    m = re.fullmatch(r"(YES|Y|NO|N)\s*#?\s*(\d+)?", t)
    if m:
        return handle_decision(m.group(1).startswith("Y"), m.group(2))
    if t in ("PAUSE", "STOP"):
        with lock:
            state["paused"] = True
            cleared = dict(state["pending"])
            state["pending"].clear()
            save_state()
        for s in cleared.values():
            close_message(s, "⏸ Paused, no order sent.")
        return "⏸ Paused. No new setups will be sent. /resume to start again."
    if t == "RESUME":
        with lock:
            state["paused"] = False
            save_state()
        return "▶️ Resumed. You'll get setups again."
    if t == "FLAT":
        ok, detail = crosstrade({"command": "closeposition", "account": NT_ACCOUNT, "instrument": NT_INSTRUMENT})
        return (f"🛑 FLAT sent for {NT_INSTRUMENT} on {NT_ACCOUNT} ({detail})." if ok
                else f"⚠️ FLAT FAILED: {detail}. Close it in NinjaTrader NOW.")
    if t in ("STATUS", "START", "ID", "HELP"):
        with lock:
            drop_expired()
            waiting = "\n".join(f"#{k} {headline(s)}" for k, s in state["pending"].items()) or "none"
        return (f"{'⏸ PAUSED' if state['paused'] else '▶️ Active'} | "
                f"{'DRY RUN' if DRY_RUN else 'LIVE ORDERS'} on {NT_ACCOUNT}\n"
                f"Trades today: {trades_today()}/{MAX_TRADES_PER_DAY}\nWaiting: {waiting}\n\n"
                f"Commands: /status /flat /pause /resume, or type YES 7 / NO 7")
    return "Commands: /status /flat /pause /resume, or type YES 7 / NO 7"


def handle_decision(approve, sid):
    with lock:
        expired = drop_expired()
        pending = state["pending"]
        if not sid:
            if len(pending) == 1:
                sid = next(iter(pending))
            elif not pending:
                sid = None
            else:
                return "More than one setup is waiting. Use the buttons or YES <number>."
        sig = pending.pop(sid, None) if sid else None
        if sig is None:
            save_state()
            result, final = (f"Setup #{sid} expired or was already handled. No order sent." if sid
                             else "Nothing is waiting (it may have expired)."), None
        elif not approve:
            save_state()
            result, final = f"Skipped #{sid}.", "❌ Skipped."
        elif trades_today() >= MAX_TRADES_PER_DAY:
            save_state()
            result, final = f"Daily limit of {MAX_TRADES_PER_DAY} reached. No order sent.", "🚫 Daily limit, no order."
        else:
            state["trades"][today()] = trades_today() + 1
            save_state()
            result, final = None, None

    for k, s in expired.items():
        close_message(s, "⌛ Expired, no order sent.")
    if sig is None or result:
        if sig is not None and final:
            close_message(sig, final)
        return result

    ok, detail = crosstrade(order_for(sig))
    if not ok:
        with lock:
            state["trades"][today()] = max(0, trades_today() - 1)
            save_state()
        close_message(sig, f"⚠️ ORDER FAILED: {detail}")
        return f"ORDER FAILED for #{sid}: {detail}. Check NinjaTrader/CrossTrade."
    side = sig["side"].upper()
    close_message(sig, f"🚀 Sent {side} {QTY} {NT_INSTRUMENT} with stop + target ({detail}). "
                       f"Trades today {trades_today()}/{MAX_TRADES_PER_DAY}. /flat to exit.")
    return f"Sent #{sid}: {side} {QTY} {NT_INSTRUMENT} ({detail})"


register_webhook()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(env("PORT", "5000")))
