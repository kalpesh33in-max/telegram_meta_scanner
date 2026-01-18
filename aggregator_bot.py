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
        return "No actionable alerts detected in the last interval."

    aggregated_data = defaultdict(lambda: {
        "actions": defaultdict(lambda: {'CE': 0, 'PE': 0}),
        "future_prices": [],
        "last_price_change": "↔"
    })

    # Regex patterns to find specific lines in the alert text
    patterns = {
        "symbol": re.compile(r"^([\w\s]+?)\s*||"),
        "action": re.compile(r"ACTION:\s*([\w\(\)-]+)"),
        "lots": re.compile(r"\((\d+)\s*lots\)"),
        "option_type": re.compile(r"STRIKE:\s*\d+(CE|PE)"),
        "future_price": re.compile(r"FUTURE PRICE:\s*([\d\.]+)"),
        "price_change": re.compile(r"PRICE:\s*(↑|↓|↔)")
    }

    for alert in alerts:
        try:
            symbol_match = patterns["symbol"].search(alert)
            action_match = patterns["action"].search(alert)
            lots_match = patterns["lots"].search(alert)
            option_type_match = patterns["option_type"].search(alert)
            future_price_match = patterns["future_price"].search(alert)
            price_change_match = patterns["price_change"].search(alert)
            
            if all([symbol_match, action_match, lots_match, option_type_match, future_price_match, price_change_match]):
                symbol = symbol_match.group(1).strip()
                if symbol == "ICICI": symbol = "ICICIBANK"
                action = action_match.group(1)
                lots = int(lots_match.group(1))
                option_type = option_type_match.group(1)
                future_price = float(future_price_match.group(1))
                price_change_indicator = price_change_match.group(1)

                data = aggregated_data[symbol]
                data["actions"][action][option_type] += lots
                data["future_prices"].append(future_price)
                data["last_price_change"] = price_change_indicator
            else:
                logger.warning(f"Failed to parse alert. Some fields were missing in: {alert[:70]}...")
        except Exception as e:
            logger.critical(f"!!!!!! UNEXPECTED ERROR DURING ALERT PARSING: {e}. Alert text: {alert[:70]}...", exc_info=True)
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
        # Adjusted padding for the symbol name
        header_line = f"SYMBOL: {symbol:<12} FUTURE PRICE: {last_price:.2f} {price_arrow}"
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
    if not update.channel_post or update.channel_post.chat.id != SOURCE_CHAT_ID:
        return
    
    message_text = update.channel_post.text
    if message_text:
        async with BUFFER_LOCK:
            MESSAGE_BUFFER.append(message_text)
        logger.info(f"Buffered 1 message from {SOURCE_CHAT_ID}.")

async def aggregation_task(app: Application):
    try:
        await app.bot.send_message(TARGET_CHAT_ID, "✅ Final Aggregator Bot (v7) is LIVE. Aggregation task started.")
    except TelegramError as e:
        logger.warning(f"Could not send startup message from aggregation_task: {e}")

    while True:
        try:
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
                    logger.error(f"Failed to send summary message: {e}")
            else:
                logger.info("Buffer is empty. Nothing to send.")
        except Exception as e:
            logger.critical(f"!!!!!! UNEXPECTED ERROR IN AGGREGATION TASK: {e} !!!!!!", exc_info=True)


async def post_start(app: Application):
    asyncio.create_task(aggregation_task(app))

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error(f"Update {update} caused error {context.error}", exc_info=True)

# =========================
# MAIN
# =========================
def main():
    logger.info("🚀 Starting Final Aggregator Bot (v7)...")
    app = ApplicationBuilder().token(BOT_TOKEN).post_init(post_start).build()
    
    app.add_handler(MessageHandler(filters.ALL, message_handler))
    app.add_error_handler(error_handler)
    
    app.run_polling()

if __name__ == "__main__":
    main()
