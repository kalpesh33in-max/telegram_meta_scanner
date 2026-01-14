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
    Parses a list of detailed, multi-line alert messages from gfdl_scanner.py,
    creates a structured summary for each symbol in a table format, inspired by lalo.pdf.
    """
    if not alerts:
        return ""

    aggregated_data = defaultdict(lambda: {
        "actions": defaultdict(lambda: {'CE': 0, 'PE': 0}),
        "future_prices": [],
        "last_price_change": "↔"  # Default to sideways
    })

    patterns = {
        "symbol": re.compile(r"^([\w\s]+)\s*\|"),
        "action": re.compile(r"ACTION: ([\w\(\)]+)"),
        "lots": re.compile(r"\((\d+) lots\)"),
        "option_type": re.compile(r"STRIKE: \d+(CE|PE)"),
        "future_price": re.compile(r"FUTURE PRICE: ([\d\.]+)"),
        "price_change": re.compile(r"PRICE: (↑|↓|↔)")
    }

    for alert in alerts:
        try:
            # Split alert into lines for easier parsing
            lines = alert.strip().split('\n')
            
            # Extract data from the multi-line format
            symbol_match = patterns["symbol"].search(lines[0])
            
            # Find the relevant lines for other fields
            action_line = next((line for line in lines if "ACTION:" in line), None)
            lots_line = next((line for line in lines if "lots" in line), None)
            option_type_line = next((line for line in lines if "STRIKE:" in line), None)
            future_price_line = next((line for line in lines if "FUTURE PRICE:" in line), None)
            price_change_line = next((line for line in lines if "PRICE:" in line), None)
            
            action_match = patterns["action"].search(action_line) if action_line else None
            lots_match = patterns["lots"].search(lots_line) if lots_line else None
            option_type_match = patterns["option_type"].search(option_type_line) if option_type_line else None
            future_price_match = patterns["future_price"].search(future_price_line) if future_price_line else None
            price_change_match = patterns["price_change"].search(price_change_line) if price_change_line else None

            if all([symbol_match, action_match, lots_match, option_type_match, future_price_match, price_change_match]):
                symbol = symbol_match.group(1).strip()
                if symbol == "ICICI": symbol = "ICICIBANK" # Normalize symbol
                action = action_match.group(1)
                lots = int(lots_match.group(1))
                option_type = option_type_match.group(2)
                future_price = float(future_price_match.group(1))
                price_change_indicator = price_change_match.group(1)

                data = aggregated_data[symbol]
                data["actions"][action][option_type] += lots
                data["future_prices"].append(future_price)
                data["last_price_change"] = price_change_indicator

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
        
        last_price = prices[-1]
        price_arrow = data["last_price_change"]

        # --- Formatting inspired by lalo.pdf ---
        # Header
        header_line1 = f"SYMBOL: {symbol}"
        header_line2 = f"FUTURE PRICE: {last_price:.2f} {price_arrow}"
        
        # Table lines
        table_lines = [
            f"{'ACTION':<18} {'CE LOTS':<10} {'PE LOTS':<10}",
            f"{'-'*18:<18} {'-'*10:<10} {'-'*10:<10}"
        ]
        
        action_order = [
            "HEDGING", "REMOVE FROM HEDGE", "BUYER(LONG)", 
            "WRITER(SHORT)", "REMOVE FROM SHORT", "REMOVE FROM LONG"
        ]
        
        for action in action_order:
            if action in actions:
                ce_lots = actions[action].get('CE', 0)
                pe_lots = actions[action].get('PE', 0)
                if ce_lots > 0 or pe_lots > 0:
                    table_lines.append(f"{action:<18} {ce_lots:<10} {pe_lots:<10}")

        # Only create a summary if there are actions to show
        if len(table_lines) > 2:
            symbol_summary = f"{header_line1}\n{header_line2}\n" + "\n".join(table_lines)
            # Wrap in Markdown code block for monospaced font
            final_summary_parts.append(f"```\n{symbol_summary}\n```")

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
                        parse_mode="MarkdownV2"
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
