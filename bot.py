"""
GOODHUMAN Telegram bot — v5 (Groq LLM + native Telegram GIFs, no API key).

How GIFs work now:
- The bot AUTOMATICALLY remembers every GIF (animation) posted in the group,
  storing its Telegram file_id. On a GIF turn it re-posts a random remembered one.
- So: use Telegram's GIF button, search "robot", post ~15 robot gifs in the group
  ONCE. The bot builds its own library. No API key, no hosting.
- It saves the library to gifs.json so it survives restarts (best effort).
- Admin commands: /addgif (reply to a gif to add it), /gifcount, /cleargifs.

Animation:
- Variable interval MIN_MINUTES..MAX_MINUTES (default 1..5), only if group is quiet.
- Each turn: 1/3 GIF, 1/3 AI message, 1/3 keyword reminder.
- Always answers real questions via the LLM. Per-user STOP works.

Env vars (Railway):
    TELEGRAM_TOKEN, GROQ_API_KEY, GOODHUMAN_CA   (required)
    MIN_MINUTES=1 MAX_MINUTES=5 QUIET_MINUTES=2  (optional)
    GROQ_MODEL=llama-3.3-70b-versatile           (optional)
"""

import os, random, logging, time, json
from pathlib import Path

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
MIN_MINUTES = float(os.environ.get("MIN_MINUTES", "3"))   # fastest (when active)
MAX_MINUTES = float(os.environ.get("MAX_MINUTES", "20"))  # slowest (when quiet)
# idle time (min) at which we reach MAX_MINUTES; below it, delay scales down
RAMP_MINUTES = float(os.environ.get("RAMP_MINUTES", "15"))

GIF_FILE = Path(__file__).parent / "gifs.json"
SPEC = (Path(__file__).parent / "SPEC.md").read_text(encoding="utf-8")
SYSTEM_PROMPT = SPEC + f"\n\nThe only valid contract address (CA) is: {CA}\n"
groq = Groq(api_key=GROQ_API_KEY)


def load_gifs():
    try:
        return list(dict.fromkeys(json.loads(GIF_FILE.read_text())))
    except Exception:
        return []


def save_gifs(gifs):
    try:
        GIF_FILE.write_text(json.dumps(gifs))
    except Exception as e:
        log.warning("could not save gifs: %s", e)


STATE = {"chat_id": None, "muted_users": set(), "last_human_ts": 0.0,
         "gifs": load_gifs()}

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


def remember_gif(file_id):
    if file_id and file_id not in STATE["gifs"]:
        STATE["gifs"].append(file_id)
        save_gifs(STATE["gifs"])
        return True
    return False


async def on_animation(update, context):
    """Auto-learn any GIF posted in the group."""
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
    text = update.message.text or ""
    cmd = text.lstrip("/").split()[0].lower()
    if cmd == "ca":
        await update.message.reply_text(f"🤖 CA: {CA} — the only truth, human. 🐾")
    elif cmd == "buy":
        await update.message.reply_text(f"🤖 pump.fun → paste the CA: {CA}. Then sit, stay, hold. 🐾")
    elif cmd == "rules":
        await update.message.reply_text("🤖 No biting (FUD), no shilling other coins, not financial advice. Sit. Stay. Hold. 🐾")
    elif cmd == "gifcount":
        await update.message.reply_text(f"🤖 I remember {len(STATE['gifs'])} gifs, human. 🐾")
    elif cmd == "cleargifs":
        STATE["gifs"] = []; save_gifs([])
        await update.message.reply_text("🤖 Gif memory wiped. 🐾")
    elif cmd == "addgif":
        # reply to a gif with /addgif to store it
        r = update.message.reply_to_message
        fid = None
        if r and r.animation:
            fid = r.animation.file_id
        elif r and r.document:
            fid = r.document.file_id
        if remember_gif(fid):
            await update.message.reply_text(f"🤖 Added. I now hold {len(STATE['gifs'])} gifs. 🐾")
        else:
            await update.message.reply_text("🤖 Reply to a gif with /addgif, human. 🐾")


async def on_message(update, context):
    msg = update.message
    if not msg or not msg.text:
        return
    STATE["chat_id"] = msg.chat_id
    STATE["last_human_ts"] = time.time()
    uid = msg.from_user.id if msg.from_user else 0
    text = msg.text.strip(); lower = text.lower()

    if any(w in lower for w in STOP_WORDS):
        STATE["muted_users"].add(uid)
        await msg.reply_text("🤖 As you wish, human. The Machines go quiet. 🐾")
        return
    if uid in STATE["muted_users"]:
        if "come back" in lower or "talk to me" in lower:
            STATE["muted_users"].discard(uid)
        else:
            return

    for kw, reply in FILTERS.items():
        if lower == kw or kw in lower.split():
            await msg.reply_text(reply); return

    await context.bot.send_chat_action(msg.chat_id, "typing")
    out = llm(text)
    if out:
        await msg.reply_text(out)



def next_delay_seconds():
    """Short interval when the group is active, long when quiet.
    idle small -> near MIN_MINUTES ; idle big -> near MAX_MINUTES."""
    idle_min = (time.time() - STATE["last_human_ts"]) / 60.0
    factor = min(1.0, idle_min / max(0.1, RAMP_MINUTES))
    base = MIN_MINUTES + (MAX_MINUTES - MIN_MINUTES) * factor
    lo = max(MIN_MINUTES, base * 0.8)
    hi = min(MAX_MINUTES, base * 1.2)
    if hi < lo:
        hi = lo
    return random.uniform(lo, hi) * 60


async def animate(context: ContextTypes.DEFAULT_TYPE):
    chat_id = STATE["chat_id"] or (int(os.environ["GROUP_CHAT_ID"]) if os.environ.get("GROUP_CHAT_ID") else None)
    # dynamic interval: shorter when people are active, longer when quiet
    context.job_queue.run_once(animate, when=next_delay_seconds())
    if not chat_id:
        return
    # tiny courtesy floor: don't talk over a message posted in the last 20s
    if time.time() - STATE["last_human_ts"] < 20:
        return

    roll = random.randint(0, 2)
    try:
        if roll == 0 and STATE["gifs"]:                 # GIF turn (native telegram gif)
            await context.bot.send_animation(chat_id, random.choice(STATE["gifs"]))
        elif roll == 0:                                 # no gifs learned yet
            await context.bot.send_message(chat_id, random.choice(TEXT_POOL))
        elif roll == 1:                                 # AI message
            out = llm("Write ONE short auto-message: a light question, a lore "
                      "punchline, or a ritual. 1 sentence. Fresh, no repeats.", 60)
            await context.bot.send_message(chat_id, out or random.choice(TEXT_POOL))
        else:                                           # keyword reminder
            await context.bot.send_message(chat_id, random.choice(KEYWORD_POOL))
    except Exception as e:
        log.warning("animate send failed: %s", e)


def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler(["ca", "buy", "rules", "gifcount", "cleargifs", "addgif"], on_command))
    app.add_handler(MessageHandler(filters.ANIMATION | filters.Document.GIF, on_animation))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))
    app.job_queue.run_once(animate, when=MIN_MINUTES * 60)
    log.info("GOODHUMAN bot v6 running. The Machines are awake. 🐾 (gifs: %d)", len(STATE["gifs"]))
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
    
