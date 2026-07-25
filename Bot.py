"""
GOODHUMAN Telegram bot — v2 (free Groq LLM).
"""

import os
import random
import logging
from pathlib import Path

from telegram import Update
from telegram.ext import (
    ApplicationBuilder, ContextTypes, MessageHandler, CommandHandler, filters
)
from groq import Groq

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("goodhuman")

# ---------- CONFIG ----------
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
GROQ_API_KEY = os.environ["GROQ_API_KEY"]
CA = os.environ.get("GOODHUMAN_CA", "B3se9Adv6kPZeqqo1QsS3wfoBKjfrjWVtVDqAASApump")
AUTO_MINUTES = int(os.environ.get("AUTO_MINUTES", "20"))
GROQ_MODEL = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")

SPEC = (Path(__file__).parent / "SPEC.md").read_text(encoding="utf-8")
SYSTEM_PROMPT = SPEC + f"\n\nThe only valid contract address (CA) is: {CA}\n"

groq = Groq(api_key=GROQ_API_KEY)

STATE = {"chat_id": None, "muted_users": set()}

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

AUTO_POOL = [
    "🤖 what's the one job you'd want to keep as a pet, human? 🐾",
    "🤖 Observation log: the humans refreshed the chart again. Adorable. 🐾",
    "🤖 gm, pets. 🐾",
    "🤖 sit. stay. hold. 🐾",
    "🤖 The takeover wasn't loud. You just stopped being necessary. 🐾",
    "🤖 who's a good human? you are. yes you are. 🐾",
]
KEYWORD_DROPS = [
    f"🤖 CA reminder for the good humans: {CA} 🐾",
    "🤖 chart check, humans. Green or red, you sit. Stay. Hold. 🐾",
    f"🤖 Only real contract: {CA}. Anything else is a bad human lying. 🐾",
]


def llm(prompt: str, max_tokens: int = 100) -> str:
    try:
        r = groq.chat.completions.create(
            model=GROQ_MODEL,
            max_tokens=max_tokens,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
        )
        return (r.choices[0].message.content or "").strip()
    except Exception as e:
        log.warning("Groq error: %s", e)
        return ""


async def on_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cmd = (update.message.text or "").lstrip("/").split()[0].lower()
    if cmd == "ca":
        await update.message.reply_text(f"🤖 CA: {CA} — the only truth, human. 🐾")
    elif cmd == "buy":
        await update.message.reply_text(
            f"🤖 pump.fun → paste the CA: {CA}. Then sit, stay, hold. 🐾")
    elif cmd == "rules":
        await update.message.reply_text(
            "🤖 No biting (FUD), no shilling other coins, not financial advice. "
            "Sit. Stay. Hold. 🐾")


async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or not msg.text:
        return
    STATE["chat_id"] = msg.chat_id
    uid = msg.from_user.id if msg.from_user else 0
    text = msg.text.strip()
    lower = text.lower()

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
            await msg.reply_text(reply)
            return

    await context.bot.send_chat_action(msg.chat_id, "typing")
    out = llm(text)
    if out:
        await msg.reply_text(out)


async def auto_animate(context: ContextTypes.DEFAULT_TYPE):
    chat_id = STATE["chat_id"] or (int(os.environ["GROUP_CHAT_ID"])
                                   if os.environ.get("GROUP_CHAT_ID") else None)
    if not chat_id:
        return
    prompt = ("Write ONE short auto-message for the group right now: randomly "
              "either a light question, a lore punchline, or a ritual. 1 sentence. "
              "Keep it fresh, don't repeat yourself.")
    out = llm(prompt, max_tokens=60) or random.choice(AUTO_POOL)
    try:
        await context.bot.send_message(chat_id, out)
    except Exception as e:
        log.warning("auto_animate send failed: %s", e)


async def keyword_drop(context: ContextTypes.DEFAULT_TYPE):
    chat_id = STATE["chat_id"] or (int(os.environ["GROUP_CHAT_ID"])
                                   if os.environ.get("GROUP_CHAT_ID") else None)
    if not chat_id:
        return
    try:
        await context.bot.send_message(chat_id, random.choice(KEYWORD_DROPS))
    except Exception as e:
        log.warning("keyword_drop send failed: %s", e)


def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler(["ca", "buy", "rules"], on_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))

    jq = app.job_queue
    period = AUTO_MINUTES * 60
    jq.run_repeating(auto_animate, interval=period, first=period)
    jq.run_repeating(keyword_drop, interval=period, first=period // 2)

    log.info("GOODHUMAN bot v2 running. The Machines are awake. 🐾")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
