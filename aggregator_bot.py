import os
import asyncio
import logging
import re
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, filters

# Setup logging
logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# Config
try:
    BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
    SOURCE_CHAT_ID = int(os.environ["SOURCE_CHAT_ID"])
    TARGET_CHAT_ID = int(os.environ["TARGET_CHAT_ID"])
    AGGREGATION_INTERVAL = int(os.getenv("AGGREGATION_INTERVAL", 5))
except KeyError as e:
    logger.critical(f"Missing Env Var: {e}")
    raise SystemExit

MESSAGE_BUFFER = []
BUFFER_LOCK = asyncio.Lock()

def get_moneyness(symbol, fut_price):
    """Determines ITM/ATM/OTM based on strike and future price."""
    try:
        # Extracts strike (e.g., 2661000 from BANKNIFTY24FEB2661000CE)
        strike_match = re.search(r"(\d{5,7})", symbol)
        if not strike_match: return ""
        strike = float(strike_match.group(1))
        opt_type = "CE" if "CE" in symbol else "PE"
        
        # 0.1% ATM Band
        if abs(strike - fut_price) <= (fut_price * 0.001): return "ATM"
        if opt_type == "CE":
            return "ITM" if strike < fut_price else "OTM"
        return "ITM" if strike > fut_price else "OTM"
    except: return ""

def process_alerts(alerts):
    passed = []
    # Updated Regex to match your GDFL_RAW_ALERTS format exactly
    patterns = {
        "action": re.compile(r"🚨 (.*?)\n"),
        "symbol": re.compile(r"Symbol:\n(.*?)\n"),
        "lots": re.compile(r"LOTS:\s*(\d+)"),
        "price": re.compile(r"PRICE:\s*([\d\.]+)"),
        "fut": re.compile(r"FUTURE PRICE:\s*([\d\.]+)"),
        "oi_chg": re.compile(r"OI CHANGE\s+:\s*([+-]?[0-9,]+)")
    }

    for alert in alerts:
        try:
            # Extraction
            act_m = patterns["action"].search(alert)
            sym_m = patterns["symbol"].search(alert)
            lot_m = patterns["lots"].search(alert)
            pr_m = patterns["price"].search(alert)
            fut_m = patterns["fut"].search(alert)
            oi_m = patterns["oi_chg"].search(alert)

            if not (sym_m and pr_m and oi_m): continue

            symbol = sym_m.group(1).strip()
            price = float(pr_m.group(1))
            oi_change_str = oi_m.group(1).replace(",", "")
            oi_change = int(oi_change_str)
            
            # Turnover Calculation
            turnover = abs(oi_change * price)
            
            # --- 1 CRORE FILTER ---
            if turnover < 10000000: continue
            
            turnover_cr = turnover / 10000000
            action = act_m.group(1).strip() if act_m else "SIGNAL"
            lots = lot_m.group(1) if lot_m else "N/A"
            
            # Moneyness
            money_label = ""
            if fut_m:
                money_label = f"| **{get_moneyness(symbol, float(fut_m.group(1)))}**"

            # Build Final Message
            formatted = (
                f"🏷 **{symbol}**\n"
                f"⚡ **{action}** {money_label}\n"
                f"💰 **Turnover: ₹{turnover_cr:.2f} Cr**\n"
                f"📦 **Lots:** {lots} | **OI Chg:** {oi_change:,}\n"
                f"📊 Price: {price}"
            )
            passed.append(formatted)
        except Exception as e:
            logger.error(f"Error parsing: {e}")
            continue
            
    return "\n\n---\n\n".join(passed)

async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    m = update.channel_post or update.message
    if m and str(m.chat.id) == str(SOURCE_CHAT_ID) and m.text:
        async with BUFFER_LOCK:
            MESSAGE_BUFFER.append(m.text)

async def aggregation_task(app):
    while True:
        await asyncio.sleep(AGGREGATION_INTERVAL)
        async with BUFFER_LOCK:
            if not MESSAGE_BUFFER: continue
            batch = list(MESSAGE_BUFFER); MESSAGE_BUFFER.clear()
        
        summary = process_alerts(batch)
        if summary:
            await app.bot.send_message(TARGET_CHAT_ID, summary, parse_mode="Markdown")

async def post_init(app):
    asyncio.create_task(aggregation_task(app))

if __name__ == "__main__":
    app = ApplicationBuilder().token(BOT_TOKEN).post_init(post_init).build()
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), message_handler))
    app.run_polling()
