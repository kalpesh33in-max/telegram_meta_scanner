import os
import re
import time
from collections import defaultdict, deque
from telegram import Bot

# =========================
# ENV VARIABLES
# =========================
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
SOURCE_CHAT_ID = int(os.getenv("SOURCE_CHAT_ID"))
TARGET_CHAT_ID = int(os.getenv("TARGET_CHAT_ID"))

bot = Bot(token=BOT_TOKEN)

# =========================
# STORAGE
# =========================
bucket = defaultdict(list)            # 1-minute bucket
score_history = defaultdict(lambda: deque(maxlen=3))
last_eval_time = int(time.time())

# =========================
# REGEX PATTERNS (YOUR FORMAT)
# =========================
OPTION_PATTERN = re.compile(
    r"(?P<symbol>[A-Z]+).*?"
    r"(?P<strike>\d+)(?P<type>CE|PE).*?"
    r"(?P<money>ITM|ATM|OTM).*?"
    r"ACTION:\s(?P<action>SHORT COVERING|LONG UNWINDING|LONG BUILD-UP|OPTION WRITING|BUYERS DOMINANT).*?"
    r"SIZE:\s(?P<size>LOW|MEDIUM|HIGH|EXTREME|VERY HIGH)",
    re.S
)

FUTURE_PRICE_PATTERN = re.compile(
    r"(?P<symbol>[A-Z]+).*?FUTURE.*?(?P<direction>↑|↓|↔)",
    re.S
)

# =========================
# SCORING TABLES
# =========================
SIZE_SCORE = {
    "EXTREME": 1.0,
    "VERY HIGH": 1.0,
    "HIGH": 0.5,
    "MEDIUM": 0.0,
    "LOW": 0.0
}

ACTION_SCORE = {
    "SHORT COVERING": 1.0,
    "LONG BUILD-UP": 1.0,
    "BUYERS DOMINANT": 1.0,
    "OPTION WRITING": 0.5,
    "LONG UNWINDING": 0.5
}

# =========================
# PARSER
# =========================
def parse_message(text):
    opt = OPTION_PATTERN.search(text)
    fut = FUTURE_PRICE_PATTERN.search(text)

    if opt:
        return {
            "symbol": opt.group("symbol"),
            "instrument": opt.group("type"),
            "money": opt.group("money"),
            "action": opt.group("action"),
            "size": opt.group("size")
        }

    if fut:
        return {
            "symbol": fut.group("symbol"),
            "instrument": "FUT",
            "direction": fut.group("direction")
        }

    return None

# =========================
# EVALUATION LOGIC
# =========================
def evaluate(symbol, data):
    score = 0
    future_bias = "NEUTRAL"

    itm_ce_strength = 0
    itm_pe_strength = 0
    itm_ce_unwind = 0
    itm_pe_unwind = 0

    for item in data:
        # FUTURE PRICE
        if item["instrument"] == "FUT":
            if item["direction"] == "↑":
                future_bias = "BULLISH"
                score += 2
            elif item["direction"] == "↓":
                future_bias = "BEARISH"
                score += 2
            else:
                future_bias = "NEUTRAL"

        # OPTIONS
        else:
            if item["money"] == "ITM":
                score += SIZE_SCORE.get(item["size"], 0)
                score += ACTION_SCORE.get(item["action"], 0)

                if item["instrument"] == "CE":
                    if item["action"] in ["SHORT COVERING", "LONG BUILD-UP", "BUYERS DOMINANT"]:
                        itm_ce_strength += 1
                    if item["action"] == "LONG UNWINDING":
                        itm_ce_unwind += 1

                if item["instrument"] == "PE":
                    if item["action"] in ["SHORT COVERING", "LONG BUILD-UP", "BUYERS DOMINANT"]:
                        itm_pe_strength += 1
                    if item["action"] == "LONG UNWINDING":
                        itm_pe_unwind += 1

    # =====================
    # TREND DECISION
    # =====================
    trend = "NO TRADE"

    if future_bias == "BULLISH" and itm_ce_strength >= 2:
        trend = "BULLISH"

    if future_bias == "BEARISH" and itm_pe_strength >= 2:
        trend = "BEARISH"

    # =====================
    # 📉 REVERSAL WARNING
    # =====================
    reversal = False

    if future_bias == "BULLISH" and itm_ce_unwind >= 2:
        reversal = True

    if future_bias == "BEARISH" and itm_pe_unwind >= 2:
        reversal = True

    # Score collapse check
    history = score_history[symbol]
    if history and history[-1] >= 5 and score <= 3:
        reversal = True

    score_history[symbol].append(score)

    return trend, score, reversal

# =========================
# EMOJI MAPPER
# =========================
def trend_emoji(score, reversal):
    if reversal:
        return "📉⚠️"
    if score >= 5:
        return "🚀🔥"
    if score >= 3:
        return "🟢🟡"
    return "⚪"

# =========================
# MAIN LOOP
# =========================
def run():
    global last_eval_time

    while True:
        updates = bot.get_updates(timeout=10)

        for update in updates:
            if not update.message:
                continue
            if update.message.chat.id != SOURCE_CHAT_ID:
                continue

            parsed = parse_message(update.message.text)
            if parsed:
                bucket[parsed["symbol"]].append(parsed)

        now = int(time.time())

        if now - last_eval_time >= 60:
            for symbol, data in bucket.items():
                trend, score, reversal = evaluate(symbol, data)

                if score >= 3 and trend != "NO TRADE":
                    msg = (
                        f"{trend_emoji(score, reversal)} {symbol} – {trend}\n"
                        f"Confidence Score: {round(score,1)}\n\n"
                        f"Logic:\n"
                        f"• Future price confirmed\n"
                        f"• ITM option participation\n"
                    )

                    if reversal:
                        msg += "\n⚠️ Reversal Risk: Trail / Book profit"

                    bot.send_message(chat_id=TARGET_CHAT_ID, text=msg)

            bucket.clear()
            last_eval_time = now

        time.sleep(5)

# =========================
# START
# =========================
if __name__ == "__main__":
    run()
