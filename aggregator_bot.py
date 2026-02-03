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
    AGGREGATION_INTERVAL_SECONDS = int(os.getenv("AGGREGATION_INTERVAL", 10))
except (KeyError, ValueError) as e:
    logger.critical(f"❌ Critical Error: Environment variable {e} is not set or invalid.")
    raise SystemExit(f"Stopping bot. Please set a valid {e} environment variable.")

MESSAGE_BUFFER = []
BUFFER_LOCK = asyncio.Lock()

# =========================
# MESSAGE SUMMARIZER
# =========================
def summarize_alerts(alerts: list[str]) -> str:
    logger.info(f"Filtering {len(alerts)} received alerts.")
    
    passed_alerts = []
    
    patterns = {
        "symbol": re.compile(r"Symbol: (.*?)\n"),
        "action": re.compile(r"🚨 (.*?)\n"),
        "oi_change": re.compile(r"OI CHANGE\s+:\s*([+-]?[0-9,]+)"),
        "price": re.compile(r"PRICE:\s*([\d\.]+)"),
    }

    for alert in alerts:
        try:
            symbol_match = patterns["symbol"].search(alert)
            if not symbol_match:
                logger.warning(f"Could not parse symbol from alert: {alert[:70]}...")
                continue
            
            symbol = symbol_match.group(1).strip()

            # New logic for Future alerts: only forward if it's a "BLAST"
            if symbol.endswith("-I"):
                if "🚀 BLAST 🚀" in alert:
                    logger.info(f"Forwarding BLAST future alert for {symbol}.")
                    passed_alerts.append(alert)
                else:
                    logger.info(f"Skipping non-BLAST future alert for {symbol}.")
                continue

            # Existing logic for Option alerts
            action_match = patterns["action"].search(alert)
            oi_change_match = patterns["oi_change"].search(alert)
            price_match = patterns["price"].search(alert)

            if not all([action_match, oi_change_match, price_match]):
                logger.warning(f"Could not parse required fields for option alert: {alert[:70]}...")
                continue

            action = action_match.group(1).strip()
            price = float(price_match.group(1))
            oi_change = int(oi_change_match.group(1).replace(",", ""))
            
            turnover_value = oi_change * price
            
            should_forward = False
            
            if turnover_value >= 10000000:
                should_forward = True
                logger.info(f"Option alert for {symbol} meets positive turnover criteria: {turnover_value:,.0f}")
            elif action.upper() in ["LONG UNWINDING", "SHORT COVERING"] and abs(turnover_value) >= 10000000:
                should_forward = True
                logger.info(f"Option alert for {symbol} meets unwinding/covering turnover criteria: {turnover_value:,.0f}")
            
            if should_forward:
                passed_alerts.append(alert)
            else:
                logger.info(f"Skipping option alert for {symbol} due to low turnover or not meeting action criteria: {turnover_value:,.0f}")

        except Exception as e:
            logger.error(f"Error processing alert: {e}. Alert text: {alert[:70]}...", exc_info=True)
            continue
            
    if not passed_alerts:
        return "" 
        
    return "\n\n---\n\n".join(passed_alerts)

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
                
                # Only send a message if there is content to send
                if summary_message:
                    try:
                        # Use HTML parse mode for better formatting control if needed, though Markdown is fine
                        await app.bot.send_message(chat_id=TARGET_CHAT_ID, text=summary_message, parse_mode="Markdown")
                        logger.info(f"Summary sent to {TARGET_CHAT_ID} successfully.")
                    except TelegramError as e:
                        logger.error(f"Failed to send summary message: {e}")
                else:
                    logger.info("No alerts met the criteria to be sent.")
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
