import os
import asyncio
from datetime import datetime
from collections import defaultdict
import logging

from telegram import Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    ContextTypes,
    MessageHandler,
    filters,
)
from telegram.error import TelegramError

# =========================
# LOGGING SETUP
# =========================
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# =========================
# ENV & CONFIG
# =========================
try:
    BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
    SOURCE_CHAT_ID = int(os.environ["SOURCE_CHAT_ID"])
    TARGET_CHAT_ID = int(os.environ["TARGET_CHAT_ID"])
    # Optional: set a different aggregation interval in seconds
    AGGREGATION_INTERVAL_SECONDS = int(os.getenv("AGGREGATION_INTERVAL", 60))
except (KeyError, ValueError) as e:
    logger.critical(f"❌ Critical Error: Environment variable {e} is not set or invalid.")
    raise SystemExit(f"Stopping bot. Please set a valid {e} environment variable.")

# This buffer will store messages. It's a simple list protected by a lock.
# The format will be a list of strings: ["message1", "message2", ...]
MESSAGE_BUFFER = []
BUFFER_LOCK = asyncio.Lock()

# =========================
# MESSAGE SUMMARIZER
# (Inspired by lalo.py)
# =========================
def summarize_alerts(alerts: list[str]) -> str:
    """Parses a list of alert messages and creates a single summary message."""
    if not alerts:
        return "" # Return empty string if there's nothing to report

    total_alerts = len(alerts)
    bullish_signals = 0
    bearish_signals = 0
    
    # Keyword-based analysis
    for alert in alerts:
        # Simple check for keywords indicating bullish or bearish sentiment
        if "PRICE: ↑" in alert or "LONG" in alert or "BUYING" in alert:
            bullish_signals += 1
        elif "PRICE: ↓" in alert or "SHORT" in alert or "WRITING" in alert or "UNWINDING" in alert:
            bearish_signals += 1

    # Determine overall market mood
    if bullish_signals > bearish_signals:
        mood = "📈 Trend is Bullish"
    elif bearish_signals > bullish_signals:
        mood = "📉 Trend is Bearish"
    else:
        mood = "⚠️ Market is Sideways or Mixed"

    # Format the final summary message
    now_formatted = datetime.now().strftime('%I:%M %p %d-%b-%Y')
    summary_header = f"**BNF 1-Minute Market Pulse**\n_{now_formatted}_\n\n"
    summary_body = (
        f"**Analysis:**\n"
        f" • Total Signals: {total_alerts}\n"
        f" • Bullish Signals: {bullish_signals}\n"
        f" • Bearish Signals: {bearish_signals}\n\n"
        f"**Conclusion:** {mood}"
    )
    
    # You can also include the raw alerts if you want, but it might get long.
    # To include them, you could uncomment the following lines:
    # raw_alerts_str = "\n\n---".join(alerts)
    # return f"{summary_header}{summary_body}\n\n--- Raw Alerts ---\n{raw_alerts_str}"
    
    return f"{summary_header}{summary_body}"

# =========================
# TELEGRAM BOT HANDLERS
# =========================
async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles incoming messages and adds them to the buffer if they are from the source chat."""
    # We only care about channel posts from the specific source channel
    if not update.channel_post or update.channel_post.chat_id != SOURCE_CHAT_ID:
        return

    message_text = update.channel_post.text
    if message_text:
        async with BUFFER_LOCK:
            MESSAGE_BUFFER.append(message_text)
        logger.info(f"Buffered 1 message from {SOURCE_CHAT_ID}")

async def aggregation_task(app: Application):
    """The background task that runs every X seconds to process the buffer."""
    logger.info("Aggregation task started. Will process buffer every %d seconds.", AGGREGATION_INTERVAL_SECONDS)
    while True:
        await asyncio.sleep(AGGREGATION_INTERVAL_SECONDS)
        
        alerts_to_process = []
        async with BUFFER_LOCK:
            if MESSAGE_BUFFER:
                # Copy messages from the buffer and clear it
                alerts_to_process.extend(MESSAGE_BUFFER)
                MESSAGE_BUFFER.clear()

        if alerts_to_process:
            logger.info(f"Processing {len(alerts_to_process)} alerts from buffer.")
            summary_message = summarize_alerts(alerts_to_process)
            
            if summary_message:
                try:
                    await app.bot.send_message(
                        chat_id=TARGET_CHAT_ID,
                        text=summary_message,
                        parse_mode="Markdown"
                    )
                    logger.info(f"Summary sent to {TARGET_CHAT_ID} successfully.")
                except TelegramError as e:
                    logger.error(f"Failed to send message to {TARGET_CHAT_ID}: {e}")
        else:
            logger.info("Buffer is empty. Nothing to send.")

async def post_start(app: Application):
    """A function to run after the bot has been initialized."""
    # Start the background aggregation task
    asyncio.create_task(aggregation_task(app))
    
    # Send a startup message
    startup_message = "✅ Aggregator Bot is LIVE.\n\nListening for alerts..."
    try:
        await app.bot.send_message(TARGET_CHAT_ID, startup_message)
    except TelegramError as e:
        logger.warning(f"Could not send startup message to {TARGET_CHAT_ID}. "
                       f"Please ensure the bot is an admin in the target channel. Error: {e}")

# =========================
# MAIN APPLICATION SETUP
# =========================
def main():
    """Sets up and runs the Telegram bot."""
    logger.info("🚀 Starting Aggregator Bot...")
    logger.info(f"Source Channel ID: {SOURCE_CHAT_ID}")
    logger.info(f"Target Channel ID: {TARGET_CHAT_ID}")
    logger.info(f"Aggregation Interval: {AGGREGATION_INTERVAL_SECONDS} seconds")

    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .post_init(post_start)
        .build()
    )

    # Add the handler for channel messages
    app.add_handler(MessageHandler(
        filters.ChatType.CHANNEL,
        message_handler
    ))

    # Start polling
    app.run_polling()


if __name__ == "__main__":
    main()
