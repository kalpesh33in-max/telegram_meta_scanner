import os
import asyncio
from datetime import datetime, timedelta
from collections import deque

from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    MessageHandler,
    filters,
)

# =========================
# ENV VARIABLES (Railway)
# =========================
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
SOURCE_CHAT_ID = int(os.getenv("SOURCE_CHAT_ID"))
TARGET_CHAT_ID = int(os.getenv("TARGET_CHAT_ID"))

if not BOT_TOKEN or not SOURCE_CHAT_ID or not TARGET_CHAT_ID:
    raise RuntimeError("Missing TELEGRAM_BOT_TOKEN / SOURCE_CHAT_ID / TARGET_CHAT_ID")

# =========================
# STORAGE (1-minute buffer)
# =========================
buffer = deque()
current_minute = None

# =========================
# SIMPLE INTERPRETATION LOGIC
# =========================
def interpret_messages(messages: list[str]) -> str:
    if not messages:
        return ""

    text = "\n".join(messages)

    bullish = sum("PRICE: ↑" in m or "LONG" in m for m in messages)
    bearish = sum("PRICE: ↓" in m or "SHORT" in m for m in messages)

    if bullish > bearish:
        mood = "📈 TREND BULLISH"
    elif bearish > bullish:
        mood = "📉 TREND BEARISH"
    else:
        mood = "⚠️ SIDEWAYS / MIXED"

    return f"""
🧠 **BNF 1-MIN AI SUMMARY**
⏱ Time: {datetime.now().strftime('%H:%M')}

Total Signals: {len(messages)}
Bullish: {bullish}
Bearish: {bearish}

{mood}
""".strip()


# =========================
# MESSAGE HANDLER
# =========================
async def on_channel_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global current_minute

    if not update.channel_post:
        return

    chat_id = update.channel_post.chat_id
    if chat_id != SOURCE_CHAT_ID:
        return

    msg_text = update.channel_post.text
    if not msg_text:
        return

    now_minute = datetime.now().replace(second=0, microsecond=0)

    if current_minute is None:
        current_minute = now_minute

    # If minute changes → send summary
    if now_minute > current_minute:
        summary = interpret_messages(list(buffer))
        if summary:
            await context.bot.send_message(
                chat_id=TARGET_CHAT_ID,
                text=summary,
                parse_mode="Markdown",
            )

        buffer.clear()
        current_minute = now_minute

    buffer.append(msg_text)


# =========================
# MAIN
# =========================
async def main():
    print("🚀 BNF Minute AI Scanner Started")
    print(f"Listening SOURCE_CHAT_ID: {SOURCE_CHAT_ID}")
    print(f"Sending to TARGET_CHAT_ID: {TARGET_CHAT_ID}")

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(
        MessageHandler(filters.ChatType.CHANNEL, on_channel_message)
    )

    await app.run_polling()


if __name__ == "__main__":
    asyncio.run(main())
