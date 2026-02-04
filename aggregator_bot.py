import os
import asyncio
import logging
import re
from telegram import Update
from telegram.ext import (
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
    format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# =========================
# ENV & CONFIG
# =========================
try:
    BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
    SOURCE_CHAT_ID = int(os.environ["SOURCE_CHAT_ID"])
    TARGET_CHAT_ID = int(os.environ["TARGET_CHAT_ID"])
    # Reduced to 5 seconds for "Fast" updates
    AGGREGATION_INTERVAL = int(os.getenv("AGGREGATION_INTERVAL", 5))
except KeyError as e:
    logger.critical(f"Missing Environment Variable: {e}")
    raise SystemExit

MESSAGE_BUFFER = []
BUFFER_LOCK = asyncio.Lock()

# =========================
# UTILITY: MONEYNESS & ACTION
# =========================
def get_moneyness(symbol, fut_price):
    try:
        # Extracts 5 or 6 digit strike price
        strike_match = re.search(r"(\d{5,6})", symbol)
        if not strike_match: return ""
        strike = float(strike_match.group(1))
        opt_type = "CE" if "CE" in symbol else "PE"
        
        atm_threshold = fut_price * 0.001 # 0.1% Band
        
        if abs(strike - fut_price) <= atm_threshold:
            return "ATM"
        
        if opt_type == "CE":
            return "ITM" if strike < fut_price else "OTM"
        else:
            return "ITM" if strike > fut_price else "OTM"
    except:
        return ""

def identify_participant(alert_text):
    """Identifies the market participant based on scanner text."""
    text = alert_text.upper()
    if "SHORT COVERING" in text: return "SHORT COVERING ↗️"
    if "LONG UNWINDING" in text: return "UNWINDING ⤵️"
    if "WRITER" in text or "SHORT BUILDUP" in text: return "WRITER ✍️"
    if "BUYER" in text or "LONG BUILDUP" in text: return "BUYER 🔵"
    return "N/A"

# =========================
# MESSAGE PROCESSOR
# =========================
def process_alerts(alerts):
    passed_alerts = []
    
    # Updated Regex to match your scanner format
    patterns = {
        "symbol": re.compile(r"Symbol: (.*?)\n"),
        "oi_change": re.compile(r"OI CHANGE\s+:\s*([+-]?[0-9,]+)"),
        "price": re.compile(r"PRICE:\s*([\d\.]+)"),
        "fut_price": re.compile(r"FUT PRICE:\s*([\d\.]+)")
    }

    for alert in alerts:
        try:
            sym_m = patterns["symbol"].search(alert)
            oi_m = patterns["oi_change"].search(alert)
            pr_m = patterns["price"].search(alert)
            fut_m = patterns["fut_price"].search(alert)

            if not all([sym_m, oi_m, pr_m]): continue

            symbol = sym_m.group(1).strip()
            # Logic: Only forward Future BLAST or Options > 1 Cr
            if symbol.endswith("-I"):
                if "🚀 BLAST 🚀" in alert:
                    passed_alerts.append(alert.strip())
                continue

            # Option Logic
            price = float(pr_m.group(1))
            oi_change = int(oi_m.group(1).replace(",", ""))
            turnover_val = abs(oi_change * price)

            # FILTER: ONLY ABOVE 1 CRORE
            if turnover_val < 10000000:
                continue

            turnover_cr = turnover_val / 10000000
            action = identify_participant(alert)
            
            moneyness = ""
            if fut_m:
                moneyness = f"| **{get_moneyness(symbol, float(fut_m.group(1)))}**"

            # Formatted Output
            formatted = (
                f"🏷 **{symbol}**\n"
                f"⚡ **{action}** {moneyness}\n"
                f"💰 **Turnover: ₹{turnover_cr:.2f} Cr**\n"
                f"📊 Price: {price}"
            )
            passed_alerts.append(formatted)

        except Exception as e:
            logger.error(f"Parsing error: {e}")
            continue
            
    return "\n\n---\n\n".join(passed_alerts)

# =========================
# TELEGRAM HANDLERS
# =========================
async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.channel_post or update.message
    if not msg or str(msg.chat.id) != str(SOURCE_CHAT_ID):
        return
    
    if msg.text:
        async with BUFFER_LOCK:
            MESSAGE_BUFFER.append(msg.text)

async def aggregation_task(app: ApplicationBuilder):
    while True:
        await asyncio.sleep(AGGREGATION_INTERVAL)
        async with BUFFER_LOCK:
            if not MESSAGE_BUFFER:
                continue
            to_process = list(MESSAGE_BUFFER)
            MESSAGE_BUFFER.clear()

        summary = process_alerts(to_process)
        if summary:
            try:
                await app.bot.send_message(TARGET_CHAT_ID, summary, parse_mode="Markdown")
            except TelegramError as e:
                logger.error(f"Send failed: {e}")

async def post_init(app):
    asyncio.create_task(aggregation_task(app))

def main():
    app = ApplicationBuilder().token(BOT_TOKEN).post_init(post_init).build()
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), message_handler))
    logger.info("Bot is active and filtering for > 1Cr alerts...")
    app.run_polling()

if __name__ == "__main__":
    main()
