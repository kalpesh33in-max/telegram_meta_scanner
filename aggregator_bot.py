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
    AGGREGATION_INTERVAL_SECONDS = int(os.getenv("AGGREGATION_INTERVAL", 60))
except (KeyError, ValueError) as e:
    logger.critical(f"❌ Critical Error: Environment variable {e} is not set or invalid.")
    raise SystemExit(f"Stopping bot. Please set a valid {e} environment variable.")

MESSAGE_BUFFER = []
BUFFER_LOCK = asyncio.Lock()

# =========================
# MESSAGE SUMMARIZER
# =========================
def summarize_alerts(alerts: list[str]) -> str:
    logger.info(f"Summarizer received {len(alerts)} alerts to process.")
    if not alerts:
        return ""

    aggregated_data = defaultdict(lambda: {
        "actions": defaultdict(lambda: {'CE': 0, 'PE': 0}),
        "future_prices": [],
        "last_price_change": "↔"
    })

    patterns = {
        "symbol": re.compile(r"^([\w\s]+?)\s*|?s*OPTION"),
        "action": re.compile(r"ACTION: ([\w\(\)]+)"),
        "lots": re.compile(r"\((\d+) lots\)"),
        "option_type": re.compile(r"STRIKE: \d+(CE|PE)"),
        "future_price": re.compile(r"FUTURE PRICE: ([\d\.]+)"),
        "price_change": re.compile(r"PRICE: (↑|↓|↔)")
    }

    for alert in alerts:
        try:
            lines = alert.strip().split('\n')
            symbol_match = patterns["symbol"].search(lines[0])
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
                if symbol == "ICICI": symbol = "ICICIBANK"
                action = action_match.group(1)
                lots = int(lots_match.group(1))
                option_type = option_type_match.group(2)
                future_price = float(future_price_match.group(1))
                price_change_indicator = price_change_match.group(1)

                data = aggregated_data[symbol]
                data["actions"][action][option_type] += lots
                data["future_prices"].append(future_price)
                data["last_price_change"] = price_change_indicator
            else:
                logger.warning(f"Failed to parse alert. Some fields were missing in: {alert[:50]}...")
        except Exception as e:
            logger.error(f"Critical parsing error for alert. Error: {e}. Alert text: {alert[:50]}...")
            continue
    
    final_summary_parts = []
    sorted_symbols = sorted(aggregated_data.keys())

    for symbol in sorted_symbols:
        data = aggregated_data[symbol]
        actions = data["actions"]
        prices = data["future_prices"]
        if not actions or not prices: continue
        
        last_price = prices[-1]
        price_arrow = data["last_price_change"]
        header_line = f"SYMBOL: {symbol:<10} FUTURE PRICE: {last_price:.2f} {price_arrow}"
        table_lines = [
            f"{'ACTION':<19} {'CE LOTS':<10} {'PE LOTS':<10}",
            f"{'-'*19:<19} {'-'*10:<10} {'-'*10:<10}"
        ]
        action_order = ["HEDGING", "REMOVE FROM HEDGE", "BUYER(LONG)", "WRITER(SHORT)", "REMOVE FROM SHORT", "REMOVE FROM LONG"]
        has_actions = False
        for action in action_order:
            if action in actions:
                ce_lots = actions[action].get('CE', 0)
                pe_lots = actions[action].get('PE', 0)
                if ce_lots > 0 or pe_lots > 0:
                    table_lines.append(f"{action:<19} {ce_lots:<10} {pe_lots:<10}")
                    has_actions = True
        if has_actions:
            final_summary_parts.append(f"{header_line}\n" + "\n".join(table_lines))

    if not final_summary_parts:
        return "No actionable alerts detected in the last interval."

    report_body = "\n\n".join(final_summary_parts)
    return f"```\n{report_body}\n```"

# =========================
# TELEGRAM BOT HANDLERS
# =========================
async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    This handler now catches ALL messages and filters manually.
    This is more robust than relying on library filters.
    """
    # Check if the update is a channel post. If not, ignore.
    if not update.channel_post:
        return

    # Check if the channel post is from our specific source channel. If not, ignore.
    if update.channel_post.chat.id != SOURCE_CHAT_ID:
        logger.info(f"Ignoring message from other channel: {update.channel_post.chat.id}")
        return
    
    # If we get here, it's a message from our source channel. Process it.
    message_text = update.channel_post.text
    if message_text:
        async with BUFFER_LOCK:
            MESSAGE_BUFFER.append(message_text)
        logger.info(f"Buffered 1 message from {SOURCE_CHAT_ID}.")

async def aggregation_task(app: Application):
    while True:
        await asyncio.sleep(AGGREGATION_INTERVAL_SECONDS)
        
        alerts_to_process = []
        async with BUFFER_LOCK:
            if MESSAGE_BUFFER:
                alerts_to_process.extend(MESSAGE_BUFFER)
                MESSAGE_BUFFER.clear()

        if alerts_to_process:
            logger.info(f"Processing {len(alerts_to_process)} alerts from buffer.")
            summary_message = summarize_alerts(alerts_to_process)
            try:
                await app.bot.send_message(chat_id=TARGET_CHAT_ID, text=summary_message, parse_mode="Markdown")
                logger.info(f"Summary sent to {TARGET_CHAT_ID} successfully.")
            except TelegramError as e:
                logger.error(f"Failed to send message to {TARGET_CHAT_ID}: {e}")
        else:
            logger.info("Buffer is empty. Nothing to send.")

async def post_start(app: Application):
    asyncio.create_task(aggregation_task(app))
    try:
        await app.bot.send_message(TARGET_CHAT_ID, "✅ Final Aggregator Bot (v2) is LIVE.")
    except TelegramError as e:
        logger.warning(f"Could not send startup message: {e}")

# =========================
# MAIN
# =========================
def main():
    logger.info("🚀 Starting Final Aggregator Bot (v2)...")
    app = ApplicationBuilder().token(BOT_TOKEN).post_init(post_start).build()
    
    # Use a universal handler to catch all messages and filter manually inside the function.
    app.add_handler(MessageHandler(filters.ALL, message_handler))
    
    app.run_polling()

if __name__ == "__main__":
    main()
