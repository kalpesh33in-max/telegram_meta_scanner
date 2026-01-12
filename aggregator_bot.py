import os
import asyncio
from datetime import datetime
from collections import defaultdict
import logging
import re
from zoneinfo import ZoneInfo

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
    """
    Parses a list of detailed alert messages, creates a structured summary for
    each symbol, and adds a trading signal (BUY CALL/BUY PUT).
    """
    if not alerts:
        return ""

    aggregated_data = defaultdict(lambda: {
        "actions": defaultdict(lambda: {'CE': 0, 'PE': 0}),
        "future_prices": []
    })

    patterns = {
        "symbol": re.compile(r"^(.*?)\s*\|"),
        "action": re.compile(r"ACTION: ([\w\(\)]+)"),
        "lots": re.compile(r"\((\d+) lots\)"),
        "option_type": re.compile(r"STRIKE: \d+(CE|PE)"),
        "future_price": re.compile(r"FUTURE PRICE: ([\d\.]+)")
    }

    for alert in alerts:
        try:
            symbol_match = patterns["symbol"].search(alert)
            action_match = patterns["action"].search(alert)
            lots_match = patterns["lots"].search(alert)
            option_type_match = patterns["option_type"].search(alert)
            future_price_match = patterns["future_price"].search(alert)

            if all([symbol_match, action_match, lots_match, option_type_match, future_price_match]):
                symbol = symbol_match.group(1).strip()
                if symbol == "ICICI": symbol = "ICICIBANK"
                action = action_match.group(1)
                lots = int(lots_match.group(1))
                option_type = option_type_match.group(2)
                future_price = float(future_price_match.group(1))

                data = aggregated_data[symbol]
                data["actions"][action][option_type] += lots
                data["future_prices"].append(future_price)
        except (AttributeError, ValueError, IndexError) as e:
            logger.warning(f"Could not parse alert: '{alert}'. Error: {e}")
            continue
    
    final_summary_parts = []
    sorted_symbols = sorted(aggregated_data.keys())

    for symbol in sorted_symbols:
        data = aggregated_data[symbol]
        actions = data["actions"]
        prices = data["future_prices"]

        if not actions or not prices:
            continue

        # --- Signal Calculation ---
        bullish_power = (actions["BUYER(LONG)"]["CE"] + actions["BUYER(LONG)"]["PE"] +
                         actions["REMOVE FROM SHORT"]["CE"] + actions["REMOVE FROM SHORT"]["PE"] +
                         actions["HEDGING"]["PE"] + # Writing Puts is bullish
                         actions["REMOVE FROM HEDGE"]["CE"]) # Closing Call writes is bullish

        bearish_power = (actions["WRITER(SHORT)"]["CE"] + actions["WRITER(SHORT)"]["PE"] +
                         actions["REMOVE FROM LONG"]["CE"] + actions["REMOVE FROM LONG"]["PE"] +
                         actions["HEDGING"]["CE"] + # Writing Calls is bearish
                         actions["REMOVE FROM HEDGE"]["PE"]) # Closing Put writes is bearish
        
        first_price = prices[0]
        last_price = prices[-1]
        price_arrow = "↔"
        if last_price > first_price: price_arrow = "↑"
        elif last_price < first_price: price_arrow = "↓"

        signal = "SIDEWAYS ↔️"
        if bullish_power > bearish_power * 1.2 and price_arrow != "↓":
            signal = "BUY CALL 📈"
        elif bearish_power > bullish_power * 1.2 and price_arrow != "↑":
            signal = "BUY PUT 📉"

        # --- Formatting ---
        header = f"**SYMBOL: {symbol}**\n"
        price_line = f"**FUTURE PRICE: {last_price:.2f} {price_arrow}**\n"
        signal_line = f"**SIGNAL: {signal}**\n\n"
        
        table_header = "| ACTION            | CE LOTS | PE LOTS |\n"
        table_separator = "|-------------------|---------|---------|\n"
        table_rows = []
        
        action_order = [
            "HEDGING", "REMOVE FROM HEDGE", "BUYER(LONG)", 
            "WRITER(SHORT)", "REMOVE FROM SHORT", "REMOVE FROM LONG"
        ]
        
        for action in action_order:
            if action in actions:
                ce_lots = actions[action].get('CE', 0)
                pe_lots = actions[action].get('PE', 0)
                if ce_lots > 0 or pe_lots > 0:
                    table_rows.append(f"| {action:<17} | {ce_lots:<7} | {pe_lots:<7} |\n")

        if table_rows:
            symbol_summary = header + price_line + signal_line + table_header + table_separator + "".join(table_rows)
            final_summary_parts.append(symbol_summary)

    if not final_summary_parts:
        return "No actionable alerts detected in the last interval."
        
    return "\n---\n".join(final_summary_parts)

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
