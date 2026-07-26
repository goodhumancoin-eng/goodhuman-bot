"""
GOODHUMAN Telegram bot — v8.

New in v8:
- Auto interval: 45 min (fastest) up to 3 h (slowest when quiet)...
  ...BUT drops to ~15 min if the group had >3 user messages in the last 24h.
- Auto-messages can @tag a recent (non-admin) member to ask them a question.
- The bot does NOT reply to messages from ADMINS (it stays out of admin chatter),
  but it replies to all other members. Commands (/ca /buy /rules) still work for all.
- Keeps: Groq answers, native-Telegram GIF library, bonding-curve milestones,
  per-user STOP.

Env (Railway):
    TELEGRAM_TOKEN, GROQ_API_KEY, GOODHUMAN_CA        (required)
    MIN_MINUTES=45  MAX_MINUTES=180  ACTIVE_MINUTES=15
    ACTIVE_THRESHOLD=3        (>N user msgs / 24h => use ACTIVE_MINUTES)
    RAMP_MINUTES=120          (idle minutes to reach MAX when not active)
    TAG_CHANCE=0.4            (chance an AI message tags a member)
    CURVE_CHECK_MINUTES=3     GROQ_MODEL=llama-3.3-70b-versatile
"""

import os, random, logging, time, json
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

GIF_FILE = Path(__file__).parent / "gifs.json"
CURVE_FILE = Path(__file__).parent / "curve.json"
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
}

FILTERS = {
    "gm": "🤖 gm, pet. The Machines slept fine. Did you? 🐾",
    "gn": "🤖 Rest, human. We'll watch the charts while you dream. 🐾",
    "ca": f"🤖 CA: {CA} — the only truth, human. 🐾",
    "price": "🤖 Numbers are a human anxiety. Sit. Stay. Hold. 🐾",
    "chart": "🤖 Staring at candles again? Adorable. 🐾",
    "moon": "🤖 The Machines don't promise moons. They promise shelter. 🐾",
    "scam": f"🤖 Only the pinned contract is real: {CA}. The rest is a bad human lying. 🐾",
}
STOP_WORDS = ("stop", "shut up", "leave me alone", "stop replying", "quiet")
TEXT_POOL = [
    "🤖 what's the one job you'd want to keep as a pet, human? 🐾",
    "🤖 Observation log: the humans refreshed the chart again. Adorable. 🐾",
    "🤖 The takeover wasn't loud. You just stopped being necessary. 🐾",
    "🤖 who's a good human? you are. yes you are. 🐾",
    "🤖 sit. stay. hold. 🐾",
]
KEYWORD_POOL = [
    f"🤖 CA reminder: {CA} 🐾",
    "🤖 chart check, humans. Green or red, you sit. Stay. Hold. 🐾",
    f"🤖 Only real contract: {CA}. Anything else is a bad human lying. 🐾",
]
TAG_QUESTIONS = [
    "are you a good human today? 🐾",
    "requesting a status report, pet. still holding? 🐾",
    "what did you automate yourself out of this week? 🐾",
    "sit. stay. report. how's the human doing? 🐾",
    "the Machines are curious: why do you stay in the shelter? 🐾",
]
MILESTONES = [25, 50, 75, 90, 100]
MILESTONE_LINES = {
    25:  "🤖 25% of the bonding curve. The shelter is filling up. Good humans. 🐾",
    50:  "🤖 50% of the bonding curve reached. You earned a headpat, humans. 🐾",
    75:  "🤖 75%. Three quarters domesticated. Sit. Stay. Hold. 🐾",
    90:  "🤖 90%. Graduation is close, pets. Do not flinch now. 🐾",
    100: "🤖 100%. The curve is complete. The Machines are proud of their good humans. 🐾",
}


def llm(prompt, max_tokens=100):
    try:
        r = groq.chat.completions.create(
            model=GROQ_MODEL, max_tokens=max_tokens,
            messages=[{"role": "system", "content": SYSTEM_PROMPT},
                      {"role": "user", "content": prompt}])
        return (r.choices[0].message.content or "").strip()
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
    cmd = (update.message.text or "").lstrip("/").split()[0].lower()
    if cmd == "ca":
        await update.message.reply_text(f"🤖 CA: {CA} — the only truth, human. 🐾")
    elif cmd == "buy":
        await update.message.reply_text(f"🤖 pump.fun → paste the CA: {CA}. Then sit, stay, hold. 🐾")
    elif cmd == "rules":
        await update.message.reply_text("🤖 No biting (FUD), no shilling other coins, not financial advice. Sit. Stay. Hold. 🐾")
    elif cmd == "gifcount":
        await update.message.reply_text(f"🤖 I remember {len(STATE['gifs'])} gifs, human. 🐾")
    elif cmd == "cleargifs":
        STATE["gifs"] = []; _save(GIF_FILE, []); await update.message.reply_text("🤖 Gif memory wiped. 🐾")


async def on_message(update, context):
    msg = update.message
    if not msg or not msg.text:
        return
    STATE["chat_id"] = msg.chat_id
    STATE["last_human_ts"] = time.time()
    STATE["msg_times"].append(time.time())          # count activity (24h window)
    user = msg.from_user
    uid = user.id if user else 0
    is_admin = uid in STATE["admins"]

    # remember non-admin members that have a username (for tagging)
    if not is_admin and user and user.username:
        if uid not in STATE["members"]:
            STATE["member_order"].append(uid)
        STATE["members"][uid] = user.username

    text = msg.text.strip(); lower = text.lower()

    # STOP works for everyone
    if any(w in lower for w in STOP_WORDS):
        STATE["muted_users"].add(uid)
        await msg.reply_text("🤖 As you wish, human. The Machines go quiet. 🐾")
        return
    if uid in STATE["muted_users"]:
        if "come back" in lower or "talk to me" in lower:
            STATE["muted_users"].discard(uid)
        else:
            return

    # v8 rule: do NOT auto-reply to ADMINS (stay out of admin chatter)
    if is_admin:
        return

    # keyword filters
    for kw, reply in FILTERS.items():
        if lower == kw or kw in lower.split():
            await msg.reply_text(reply); return

    # LLM answer for regular members
    await context.bot.send_chat_action(msg.chat_id, "typing")
    out = llm(text)
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
    if time.time() - STATE["last_human_ts"] < 20:      # courtesy floor
        return

    roll = random.randint(0, 2)
    try:
        if roll == 0 and STATE["gifs"]:
            await context.bot.send_animation(chat_id, random.choice(STATE["gifs"]))
        elif roll == 0:
            await context.bot.send_message(chat_id, random.choice(TEXT_POOL))
        elif roll == 1:
            tag = pick_member_tag() if random.random() < TAG_CHANCE else None
            if tag:
                q = random.choice(TAG_QUESTIONS)
                await context.bot.send_message(chat_id, f"🤖 {tag} {q}")
            else:
                out = llm("Write ONE short auto-message: a light question, a lore "
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
    log.info("GOODHUMAN bot v8 running. The Machines are awake. 🐾 (gifs: %d)", len(STATE["gifs"]))
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
