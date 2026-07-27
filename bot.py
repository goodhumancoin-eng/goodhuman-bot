"""
GOODHUMAN Telegram bot — v9.
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
    "msg_times": deque(maxlen=500),
    "members": {},
    "member_order": deque(maxlen=100),
    "admins": set(),
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
STOP_WORDS = ("stop", "shut up", "leave me alone", "stop replying", "quiet")
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


def active_rec
