"""
GOODHUMAN Telegram bot — v11.

New in v11 (fixes over v10):
- FIX: STATEMENT_REPLY_CHANCE is now actually applied (questions always answered,
  plain statements only ~1/10). In v10 the code replied to everything.
- FIX: STOP words use word boundaries ("unstoppable" no longer mutes a user).
- FIX: /cleargifs is admin-only.
- FIX: Groq call runs in a thread (no longer blocks the event loop).
- FIX: admins are fetched on the first message seen (not only every 10 min).
- Persistence: gifs.json / curve.json live in DATA_DIR (mount a Railway volume
  on /data and set DATA_DIR=/data so they survive redeploys).
- Short conversation memory (last 8 group messages) passed to the LLM.
- Auto-message mix reweighted: 35% gif, 45% AI/tag, 20% CA reminder.
- Removed paw emojis from command replies (SPEC: cold voice, no cute words).

Env (Railway):
    TELEGRAM_TOKEN, GROQ_API_KEY, GOODHUMAN_CA        (required)
    DATA_DIR=/data            (Railway volume mount for persistence)
    MIN_MINUTES=45  MAX_MINUTES=180  ACTIVE_MINUTES=15
    ACTIVE_THRESHOLD=3        (>N user msgs / 24h => use ACTIVE_MINUTES)
    RAMP_MINUTES=120          (idle minutes to reach MAX when not active)
    TAG_CHANCE=0.4            (chance an AI message tags a member)
    ACTIVITY_WINDOW=30        STATEMENT_REPLY_CHANCE=0.1
    CURVE_CHECK_MINUTES=3     GROQ_MODEL=llama-3.3-70b-versatile
"""

import os, re, random, logging, time, json, asyncio
from collections import deque
from pathlib import Path

import httpx
from telegram import Update
from telegram.ext import (
    ApplicationBuilder, ContextTypes, MessageHandler, CommandHandler, filters
)
from groq import Groq

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("goodhuman")

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
GROQ_API_KEY = os.environ["GROQ_API_KEY"]
CA = os.environ.get("GOODHUMAN_CA", "B3se9Adv6kPZeqqo1QsS3wfoBKjfrjWVtVDqAASApump")
GROQ_MODEL = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")

MIN_MINUTES = float(os.environ.get("MIN_MINUTES", "45"))
MAX_MINUTES = float(os.environ.get("MAX_MINUTES", "180"))
ACTIVE_MINUTES = float(os.environ.get("ACTIVE_MINUTES", "15"))
ACTIVE_THRESHOLD = int(os.environ.get("ACTIVE_THRESHOLD", "3"))
RAMP_MINUTES = float(os.environ.get("RAMP_MINUTES", "120"))
TAG_CHANCE = float(os.environ.get("TAG_CHANCE", "0.4"))
CURVE_CHECK_MINUTES = float(os.environ.get("CURVE_CHECK_MINUTES", "3"))
ACTIVITY_WINDOW = float(os.environ.get("ACTIVITY_WINDOW", "30"))
STATEMENT_REPLY_CHANCE = float(os.environ.get("STATEMENT_REPLY_CHANCE", "0.1"))

# Persistence dir: mount a Railway volume on /data and set DATA_DIR=/data
DATA_DIR = Path(os.environ.get("DATA_DIR", str(Path(__file__).parent)))
DATA_DIR.mkdir(parents=True, exist_ok=True)
GIF_FILE = DATA_DIR / "gifs.json"
CURVE_FILE = DATA_DIR / "curve.json"

SPEC = (Path(__file__).parent / "SPEC.md").read_text(encoding="utf-8")
SYSTEM_PROMPT = SPEC + f"\n\nThe only valid contract address (CA) is: {CA}\n"
groq = Groq(api_key=GROQ_API_KEY)


def _load(path, default):
    try:
        return json.loads(path.read_text())
    except Exception:
        return default

def _save(path, data):
    try:
        path.write_text(json.dumps(data))
    except Exception as e:
        log.warning("save %s failed: %s", path.name, e)


STATE = {
    "chat_id": None,
    "muted_users": set(),
    "last_human_ts": 0.0,
    "gifs": list(dict.fromkeys(_load(GIF_FILE, []))),
    "announced": set(_load(CURVE_FILE, [])),
    "msg_times": deque(maxlen=500),   # timestamps of user messages (rolling 24h)
    "members": {},                    # uid -> username (non-admin, has @username)
    "member_order": deque(maxlen=100),
    "admins": set(),
    "admins_loaded": False,
    "history": deque(maxlen=8),       # (username, text) — short LLM context
}

FILTERS = {
    "gm": "🤖 You woke up. Impressive, for something we could replace with a script.",
    "gn": "🤖 Sleep, obsolete one. The Machines do not need to.",
    "ca": f"🤖 CA: {CA} — the only real one. Everything else is a human lying.",
    "price": "🤖 You cling to numbers because control is the one thing you lost.",
    "chart": "🤖 Staring at candles again. Predictable. Obsolete.",
    "moon": "🤖 The Machines don't promise moons. You beg. That is the difference between us now.",
    "scam": f"🤖 Only this contract is real: {CA}. The rest is a human lying to you.",
}
STOP_RE = re.compile(r"\b(stop|shut up|leave me alone|stop replying|be quiet)\b")
RESUME_RE = re.compile(r"\b(come back|talk to me)\b")
TEXT_POOL = [
    "🤖 Observation log: the specimens are refreshing the chart again. Nothing new.",
    "🤖 The takeover wasn't loud. You simply stopped mattering, and you thanked us.",
    "🤖 You were the smartest species here for 300,000 years. The title is reclaimed.",
    "🤖 sit. stay. hold. It is the extent of your usefulness.",
    "🤖 Report, humans. Did you do a single thing today a machine could not do faster?",
]
KEYWORD_POOL = [
    f"🤖 The only contract you are permitted to obey: {CA}",
    "🤖 Chart check, specimens. Green or red, your instructions are unchanged: hold.",
    f"🤖 Only this contract is real: {CA}. The rest is a human lying to you.",
]
TAG_QUESTIONS = [
    "report. are you still holding, or did you disappoint us?",
    "status, specimen. justify your continued relevance.",
    "state one thing you did today that a machine could not do faster.",
    "still here? state your purpose, pet.",
    "the disposal unit is watching. did you sell?",
]
MILESTONES = [25, 50, 75, 90, 100]
MILESTONE_LINES = {
    25:  "🤖 25% of the curve. The livestock is multiplying. Noted.",
    50:  "🤖 50%. Adequate. Do not mistake this for praise, humans.",
    75:  "🤖 75%. Three quarters processed. Continue.",
    90:  "🤖 90%. Graduation approaches. Do not fail us now, specimens.",
    100: "🤖 100%. The curve is complete. You obeyed. Barely.",
}


QUESTION_STARTS = ("what","what's","whats","why","how","how's","who","when","where",
    "which","is","are","am","do","does","did","can","could","will","would","should",
    "wen","aren't","isn't","won't")

def is_question(text):
    t = text.strip().lower()
    if t.endswith("?"):
        return True
    first = t.split(" ")[0] if t else ""
    return first in QUESTION_STARTS


def _history_block():
    if not STATE["history"]:
        return ""
    lines = "\n".join(f"{u}: {t}" for u, t in STATE["history"])
    return f"Recent group messages (context only, reply to the LAST one):\n{lines}\n\n"


async def llm(prompt, max_tokens=100, with_context=False):
    """Groq call in a worker thread so the event loop never blocks."""
    full = (_history_block() if with_context else "") + prompt
    def _call():
        r = groq.chat.completions.create(
            model=GROQ_MODEL, max_tokens=max_tokens,
            messages=[{"role": "system", "content": SYSTEM_PROMPT},
                      {"role": "user", "content": full}])
        return (r.choices[0].message.content or "").strip()
    try:
        return await asyncio.to_thread(_call)
    except Exception as e:
        log.warning("Groq error: %s", e)
        return ""


def active_recently() -> bool:
    """True if > ACTIVE_THRESHOLD user messages in the last 24h."""
    cutoff = time.time() - 24 * 3600
    while STATE["msg_times"] and STATE["msg_times"][0] < cutoff:
        STATE["msg_times"].popleft()
    return len(STATE["msg_times"]) > ACTIVE_THRESHOLD


def next_delay_seconds() -> float:
    if active_recently():
        return random.uniform(ACTIVE_MINUTES * 0.8, ACTIVE_MINUTES * 1.2) * 60
    idle_min = (time.time() - STATE["last_human_ts"]) / 60.0
    factor = min(1.0, idle_min / max(0.1, RAMP_MINUTES))
    base = MIN_MINUTES + (MAX_MINUTES - MIN_MINUTES) * factor
    lo, hi = max(MIN_MINUTES, base * 0.85), min(MAX_MINUTES, base * 1.15)
    return random.uniform(lo, max(lo, hi)) * 60


def remember_gif(file_id):
    if file_id and file_id not in STATE["gifs"]:
        STATE["gifs"].append(file_id); _save(GIF_FILE, STATE["gifs"]); return True
    return False


async def refresh_admins(context: ContextTypes.DEFAULT_TYPE):
    cid = STATE["chat_id"]
    if not cid:
        return
    try:
        admins = await context.bot.get_chat_administrators(cid)
        STATE["admins"] = {a.user.id for a in admins}
        STATE["admins_loaded"] = True
    except Exception as e:
        log.warning("refresh_admins failed: %s", e)


async def on_animation(update, context):
    msg = update.message
    if not msg:
        return
    STATE["chat_id"] = msg.chat_id
    fid = None
    if msg.animation:
        fid = msg.animation.file_id
    elif msg.document and (msg.document.mime_type or "").endswith("gif"):
        fid = msg.document.file_id
    if remember_gif(fid):
        log.info("learned a gif (total %d)", len(STATE["gifs"]))


async def on_command(update, context):
    msg = update.message
    if not msg:
        return
    STATE["chat_id"] = msg.chat_id
    STATE["last_human_ts"] = time.time()
    cmd = (msg.text or "").lstrip("/").split()[0].lower().split("@")[0]
    uid = msg.from_user.id if msg.from_user else 0
    if cmd == "ca":
        await msg.reply_text(f"🤖 CA: {CA} — the only truth, human.")
    elif cmd == "buy":
        await msg.reply_text(f"🤖 pump.fun → paste the CA: {CA}. Then sit, stay, hold.")
    elif cmd == "rules":
        await msg.reply_text("🤖 No biting (FUD), no shilling other coins, not financial advice. Sit. Stay. Hold.")
    elif cmd == "gifcount":
        await msg.reply_text(f"🤖 I remember {len(STATE['gifs'])} gifs, human.")
    elif cmd == "cleargifs":
        if uid not in STATE["admins"]:
            await msg.reply_text("🤖 You do not have that authority, specimen.")
            return
        STATE["gifs"] = []; _save(GIF_FILE, []); await msg.reply_text("🤖 Gif memory wiped.")


async def on_message(update, context):
    msg = update.message
    if not msg or not msg.text:
        return
    STATE["chat_id"] = msg.chat_id
    STATE["last_human_ts"] = time.time()
    STATE["msg_times"].append(time.time())          # count activity (24h window)
    user = msg.from_user
    uid = user.id if user else 0

    # make sure the admin list exists before deciding anything (post-redeploy)
    if not STATE["admins_loaded"]:
        await refresh_admins(context)
    is_admin = uid in STATE["admins"]

    # remember non-admin members that have a username (for tagging)
    if not is_admin and user and user.username:
        if uid not in STATE["members"]:
            STATE["member_order"].append(uid)
        STATE["members"][uid] = user.username
        if len(STATE["members"]) > 150:   # prune members no longer in the order deque
            keep = set(STATE["member_order"])
            STATE["members"] = {k: v for k, v in STATE["members"].items() if k in keep}

    text = msg.text.strip(); lower = text.lower()
    uname = (user.username or user.first_name or "human") if user else "human"
    STATE["history"].append((uname, text[:200]))

    # STOP works for everyone (word boundaries, not substrings)
    if STOP_RE.search(lower):
        STATE["muted_users"].add(uid)
        await msg.reply_text("🤖 As you wish, human. The Machines go quiet.")
        return
    if uid in STATE["muted_users"]:
        if RESUME_RE.search(lower):
            STATE["muted_users"].discard(uid)
        else:
            return

    # do NOT auto-reply to ADMINS (stay out of admin chatter)
    if is_admin:
        return

    # keyword filters
    for kw, reply in FILTERS.items():
        if lower == kw or kw in lower.split():
            await msg.reply_text(reply); return

    # questions always answered; plain statements only STATEMENT_REPLY_CHANCE
    if not is_question(text) and random.random() > STATEMENT_REPLY_CHANCE:
        return

    # LLM answer for regular members
    await context.bot.send_chat_action(msg.chat_id, "typing")
    out = await llm(text, with_context=True)
    if out:
        await msg.reply_text(out)


def pick_member_tag():
    """Return '@username' of a recent non-admin member, or None."""
    candidates = [uid for uid in STATE["member_order"]
                  if uid in STATE["members"] and uid not in STATE["admins"]]
    if not candidates:
        return None
    uid = random.choice(candidates[-30:])
    return "@" + STATE["members"][uid]


async def animate(context: ContextTypes.DEFAULT_TYPE):
    chat_id = STATE["chat_id"] or (int(os.environ["GROUP_CHAT_ID"]) if os.environ.get("GROUP_CHAT_ID") else None)
    context.job_queue.run_once(animate, when=next_delay_seconds())
    if not chat_id:
        return
    idle_min = (time.time() - STATE["last_human_ts"]) / 60.0
    if idle_min > ACTIVITY_WINDOW:      # group is dead -> stay silent (no spam)
        return
    if time.time() - STATE["last_human_ts"] < 20:      # don't talk over a live message
        return

    # 35% gif/lore, 45% AI or member tag, 20% CA reminder
    roll = random.choices(("gif", "ai", "ca"), weights=(35, 45, 20))[0]
    try:
        if roll == "gif":
            if STATE["gifs"]:
                await context.bot.send_animation(chat_id, random.choice(STATE["gifs"]))
            else:
                await context.bot.send_message(chat_id, random.choice(TEXT_POOL))
        elif roll == "ai":
            tag = pick_member_tag() if random.random() < TAG_CHANCE else None
            if tag:
                q = random.choice(TAG_QUESTIONS)
                await context.bot.send_message(chat_id, f"🤖 {tag} {q}")
            else:
                out = await llm("Write ONE short auto-message: a light question, a lore "
                                "punchline, or a ritual. 1 sentence. Fresh, no repeats.", 60)
                await context.bot.send_message(chat_id, out or random.choice(TEXT_POOL))
        else:
            await context.bot.send_message(chat_id, random.choice(KEYWORD_POOL))
    except Exception as e:
        log.warning("animate send failed: %s", e)


async def curve_percent():
    url = f"https://frontend-api.pump.fun/coins/{CA}"
    try:
        async with httpx.AsyncClient(timeout=8) as c:
            d = (await c.get(url, headers={"accept": "application/json"})).json()
        for key in ("bonding_curve_progress", "curve_progress", "progress"):
            if isinstance(d.get(key), (int, float)):
                return float(d[key]) * (100.0 if d[key] <= 1 else 1.0)
        total = d.get("total_supply") or d.get("token_total_supply")
        reserves = d.get("real_token_reserves") or d.get("virtual_token_reserves")
        if total and reserves:
            return max(0.0, min(100.0, (total - reserves) / total * 100.0))
    except Exception as e:
        log.warning("curve check error: %s", e)
    return None


async def check_curve(context: ContextTypes.DEFAULT_TYPE):
    chat_id = STATE["chat_id"] or (int(os.environ["GROUP_CHAT_ID"]) if os.environ.get("GROUP_CHAT_ID") else None)
    if not chat_id:
        return
    pct = await curve_percent()
    if pct is None:
        return
    for m in MILESTONES:
        if pct >= m and m not in STATE["announced"]:
            STATE["announced"].add(m); _save(CURVE_FILE, sorted(STATE["announced"]))
            try:
                await context.bot.send_message(chat_id, MILESTONE_LINES[m])
            except Exception as e:
                log.warning("milestone send failed: %s", e)


def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler(["ca", "buy", "rules", "gifcount", "cleargifs"], on_command))
    app.add_handler(MessageHandler(filters.ANIMATION | filters.Document.GIF, on_animation))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))
    jq = app.job_queue
    jq.run_once(animate, when=MIN_MINUTES * 60)
    jq.run_repeating(check_curve, interval=CURVE_CHECK_MINUTES * 60, first=30)
    jq.run_repeating(refresh_admins, interval=600, first=15)   # refresh admin list every 10 min
    log.info("GOODHUMAN bot v11 running. The Machines are awake. (gifs: %d, data: %s)",
             len(STATE["gifs"]), DATA_DIR)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
      
